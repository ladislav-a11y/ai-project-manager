"""V2 scheduler/router: picks the next project to work on.

Selects the highest-priority (0-5, 5 = most urgent), unblocked project
that has at least one available provider. This is pure, local, in-memory
logic over already-fetched ProjectRecords and the ProviderRegistry's
cached state - it never calls a provider or spends an AI token just to
decide what to do next. Actual provider availability re-verification
happens separately via ``ProviderRegistry.recheck``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import ProjectRecord, ProjectStatus
from .providers import ProviderRegistry

# Statuses that represent projects the scheduler should never pick up,
# regardless of priority.
#
# ProjectStatus.TESTING (the physical "Testování" list) is deliberately
# excluded from normal implementation dispatch: a card there has its
# implementation done and is waiting only for ai-orchestrator's audit
# verdict, never for another round of ordinary agent work. Routing it back
# into pick_next_project would let it be re-sent to the implementation
# agent instead of through the audit-only path (see is_auditable /
# pick_next_audit_project below, and orchestrator_handoff.build_audit_task).
NOT_SCHEDULABLE_STATUSES = {
    ProjectStatus.DONE,
    ProjectStatus.PAUSED,
    ProjectStatus.TESTING,
    ProjectStatus.ERROR,
}

# A workflow card must reach Hotovo before a fresh card from Připraveno is
# admitted.  Testování and Čeká na AI are handled by their dedicated paths;
# if either is waiting (or blocked for human action), normal dispatch stops
# instead of silently starting another project.
WAITING_WORKFLOW_STATUSES = {
    ProjectStatus.TESTING,
    ProjectStatus.PAUSED,
    ProjectStatus.BLOCKED,
    ProjectStatus.ERROR,
}

def _is_human_hold(project: ProjectRecord) -> bool:
    return bool(project.human_action_step or project.human_notified_reason)


def _workflow_status_label(status: ProjectStatus) -> str:
    return {
        ProjectStatus.TESTING: "Testování",
        ProjectStatus.PAUSED: "Čeká na AI",
        ProjectStatus.BLOCKED: "Blokováno",
        ProjectStatus.ERROR: "Chyba",
    }.get(status, status.value)


def _waiting_project_detail(project: ProjectRecord) -> str:
    detail = f"{project.name} [{_workflow_status_label(project.status)}"
    if project.status == ProjectStatus.PAUSED:
        detail += f"; Trello retry_after={project.retry_after or 'neuvedený'}"
    elif _is_human_hold(project):
        detail += "; vyžaduje zásah člověka"
    return detail + "]"


def explain_no_implementation_dispatch(projects: list[ProjectRecord]) -> str:
    """Explain why normal implementation dispatch returned no project.

    Provider selection is owned by ai-orchestrator. This explanation therefore
    reports only PM/Trello workflow gates and must not claim that PM proved a
    provider unavailable.
    """
    waiting = [project for project in projects if project.status in WAITING_WORKFLOW_STATUSES]
    if waiting:
        corrective = any(
            project.returned_from_testing
            and project.status in {ProjectStatus.READY, ProjectStatus.IN_PROGRESS}
            and not project.is_blocked
            for project in projects
        )
        hard_wait = any(
            project.status in {ProjectStatus.TESTING, ProjectStatus.ERROR}
            or (project.status in {ProjectStatus.PAUSED, ProjectStatus.BLOCKED} and not _is_human_hold(project))
            for project in waiting
        )
        if not corrective or hard_wait:
            details = "; ".join(_waiting_project_detail(project) for project in waiting)
            return f"workflow wait blocks implementation dispatch: {details}"

    active = [project for project in projects if project.status == ProjectStatus.IN_PROGRESS]
    candidates = [
        project
        for project in (active or projects)
        if is_schedulable(project) and _dependencies_satisfied(project, projects)
    ]
    if not candidates:
        return "no project in a schedulable Trello workflow state"
    return "no implementation dispatch decision"


@dataclass
class SchedulingDecision:
    project: ProjectRecord
    provider: str


def is_schedulable(project: ProjectRecord) -> bool:
    folded_task = (project.main_task or project.orchestrator_ready_task or "").casefold()
    # Status/checkpoint cards are durable documentation, not executable work.
    # They coexist with work cards on the Trello board and must never win a
    # priority sort merely because their title starts with P4/P5. This is
    # judged from the card's actual task content, never its title text -
    # a "(čeká)" note in the name is free-form prose a human may add to
    # any card regardless of the physical Trello list it sits in, and must
    # never override that list (see ProjectRecord.is_blocked and
    # NOT_SCHEDULABLE_STATUSES below, which are the sole authority for
    # whether a card is actually waiting/blocked).
    if folded_task.startswith("účel") and "aktuální stav" in folded_task:
        return False
    if project.is_blocked:
        return False
    if project.status in NOT_SCHEDULABLE_STATUSES:
        return False
    if project.status == ProjectStatus.IN_PROGRESS:
        # Mirrors daemon._promote_completed_implementations_to_testing's own
        # completeness check. A card whose implementation DoD is already
        # fully checked has nothing left for another implementation
        # dispatch to do - it is only waiting on controller finalization
        # (commit/push) before it may be promoted to Testování. Without this
        # guard a card whose finalization failed (dirty tests, a blocked
        # push, ...) would be re-dispatched for implementation every single
        # tick, spending a real provider call for work that is already done.
        implementation_items = [item for item in project.dod if item.phase == "implementation"]
        if implementation_items and all(item.checked for item in implementation_items):
            return False
    return True


def _dependencies_satisfied(project: ProjectRecord, projects: list[ProjectRecord]) -> bool:
    """Prevent an Inbox child from overtaking an unfinished sibling."""
    metadata = (project.extra_data or {}).get("inbox_preparation")
    if not isinstance(metadata, dict):
        return True
    dependencies = metadata.get("depends_on_subtask_indices", [])
    if not dependencies:
        return True
    source_id = metadata.get("source_card_id")
    if not source_id:
        return False
    siblings = {
        (child.extra_data or {}).get("inbox_preparation", {}).get("subtask_index"): child
        for child in projects
        if isinstance((child.extra_data or {}).get("inbox_preparation"), dict)
        and (child.extra_data or {}).get("inbox_preparation", {}).get("source_card_id") == source_id
    }
    return all(siblings.get(index) is not None and siblings[index].status == ProjectStatus.DONE for index in dependencies)


def pick_next_project(
    projects: list[ProjectRecord],
    provider_registry: ProviderRegistry,
    providers_for_project: Optional[dict] = None,
    default_providers: Optional[list[str]] = None,
) -> Optional[SchedulingDecision]:
    """Return the highest-priority unblocked project paired with an
    available provider, or None if nothing is schedulable right now.

    ``providers_for_project`` optionally maps project name -> list of
    acceptable provider names (falls back to ``default_providers``, or
    to every provider registered so far).
    """
    waiting = [project for project in projects if project.status in WAITING_WORKFLOW_STATUSES]
    if waiting:
        corrective = any(
            project.returned_from_testing
            and project.status in {ProjectStatus.READY, ProjectStatus.IN_PROGRESS}
            and not project.is_blocked
            for project in projects
        )
        hard_wait = any(
            project.status in {ProjectStatus.TESTING, ProjectStatus.ERROR}
            or (project.status in {ProjectStatus.PAUSED, ProjectStatus.BLOCKED} and not _is_human_hold(project))
            for project in waiting
        )
        # A corrective return may repair unrelated human holds, but it may
        # never bypass an audit, provider wait, or unknown error state.
        if not corrective or hard_wait:
            return None

    # V2: restrict execution candidates, never the dependency evidence snapshot.
    active = [p for p in projects if p.status == ProjectStatus.IN_PROGRESS]
    candidates = [p for p in (active or projects) if is_schedulable(p) and _dependencies_satisfied(p, projects)]
    # Continue the work already visible in ``Pracuje se`` before admitting a
    # new card from ``Připraveno``.  Priority is a tie-break *within* a
    # workflow phase, not a reason to preempt an active checkpoint.  Testing
    # and Čeká na AI are handled by the audit/recovery paths before this
    # implementation selector is called.
    phase_order = {
        ProjectStatus.IN_PROGRESS: 0,
        ProjectStatus.READY: 1,
        ProjectStatus.NEW: 1,
        ProjectStatus.ERROR: 1,
    }
    candidates.sort(
        key=lambda p: (
            phase_order.get(p.status, 2),
            0 if p.returned_from_testing else 1,
            -p.priority,
            p.name.casefold(),
        )
    )

    for project in candidates:
        return SchedulingDecision(project=project, provider="provider-broker")

    return None


def is_auditable(project: ProjectRecord) -> bool:
    """Whether a project currently belongs on the audit-only path.

    Only a card physically sitting in the Testování list (``status ==
    TESTING``) is ever eligible - never anything derived from title text
    or DoD content - mirroring is_schedulable's reliance on the physical
    list as the sole authority. A TESTING card genuinely blocked (see
    ProjectRecord.is_blocked) is excluded the same way a blocked card is
    excluded from normal implementation dispatch.
    """
    if project.is_blocked:
        return False
    # An audit-only rejection remains in Testování as durable workflow
    # evidence, but it must not be sent to the same auditor again on every
    # tick.  A human or a later implementation move can clear this marker.
    if (project.extra_data or {}).get("audit_waiting_for_change") is True:
        return False
    return project.status == ProjectStatus.TESTING


def pick_next_audit_project(
    projects: list[ProjectRecord],
    provider_registry: ProviderRegistry,
    providers_for_project: Optional[dict] = None,
    default_providers: Optional[list[str]] = None,
) -> Optional[SchedulingDecision]:
    """Return the highest-priority project waiting in Testování for an
    ai-orchestrator audit verdict, paired with an available provider - or
    None if no card is currently awaiting audit.

    This is a separate selection path from ``pick_next_project`` on
    purpose: a Testování card must never compete with, or be picked up
    by, the normal implementation dispatch (see
    scheduler.NOT_SCHEDULABLE_STATUSES) - it is only ever handed to
    ai-orchestrator's audit-only mode (see
    orchestrator_handoff.build_audit_task).
    """
    candidates = [p for p in projects if is_auditable(p)]
    candidates.sort(key=lambda p: p.priority, reverse=True)

    for project in candidates:
        return SchedulingDecision(project=project, provider="provider-broker")

    return None


def explain_no_audit_dispatch(projects: list[ProjectRecord]) -> str:
    """Explain why no Testování card can enter the AO audit path."""
    if not any(project.status == ProjectStatus.TESTING for project in projects):
        return "no project in Testování awaiting ai-orchestrator audit"
    blocked = [
        _waiting_project_detail(project)
        for project in projects
        if project.status == ProjectStatus.TESTING and not is_auditable(project)
    ]
    if blocked:
        return "Testování has no unblocked audit candidate: " + "; ".join(blocked)
    return "no audit dispatch decision"


def audit_capability_key(project: ProjectRecord) -> Optional[str]:
    """Stable audit task-family key used by the provider capability ledger."""
    preparation = (project.extra_data or {}).get("inbox_preparation")
    scope = preparation.get("scope") if isinstance(preparation, dict) else None
    scope = str(scope or project.name).strip().casefold()
    project_key = str(project.project_key or "unknown").strip().casefold()
    if not scope:
        return None
    return f"audit:{project_key}:{scope}"
