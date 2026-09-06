"""Scheduler/router: picks the next project to work on.

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

# ``auto`` is a PM configuration alias, not an ai-orchestrator provider.  It
# must be expanded before provider availability is checked; otherwise PM can
# select the literal alias and later pass it to AO as ``--provider-order auto``.
# Keep the order aligned with the PM-owned failover policy.  Both the canonical
# PM Claude name and AO's legacy name are accepted because persisted provider
# state can contain either spelling.
AUTO_PROVIDER_ORDER = ("groq", "antigravity", "claude", "claude-code", "codex")


def expand_provider_aliases(
    provider_names: list[str], provider_registry: ProviderRegistry
) -> list[str]:
    """Expand PM-only provider aliases into registered provider identities."""
    expanded: list[str] = []
    registered = set(provider_registry.registered_names())
    for provider_name in provider_names:
        if provider_name.casefold() != "auto":
            if provider_name not in expanded:
                expanded.append(provider_name)
            continue
        for candidate in AUTO_PROVIDER_ORDER:
            # Prefer PM's canonical ``claude`` identity over the AO alias when
            # both happen to be present in durable provider state.
            if candidate in registered and candidate not in expanded:
                if candidate == "claude-code" and "claude" in registered:
                    continue
                expanded.append(candidate)
    return expanded


def _is_human_hold(project: ProjectRecord) -> bool:
    return bool(project.human_action_step or project.human_notified_reason)


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
    fallback_providers = expand_provider_aliases(
        default_providers or provider_registry.registered_names(), provider_registry
    )

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

    candidates = [p for p in projects if is_schedulable(p) and _dependencies_satisfied(p, projects)]
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
        allowed_providers = expand_provider_aliases(
            (providers_for_project or {}).get(project.name, fallback_providers),
            provider_registry,
        )
        for provider_name in allowed_providers:
            if provider_registry.is_available(provider_name):
                return SchedulingDecision(project=project, provider=provider_name)

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
    fallback_providers = expand_provider_aliases(
        default_providers or provider_registry.registered_names(), provider_registry
    )

    candidates = [p for p in projects if is_auditable(p)]
    candidates.sort(key=lambda p: p.priority, reverse=True)

    for project in candidates:
        allowed_providers = expand_provider_aliases(
            (providers_for_project or {}).get(project.name, fallback_providers),
            provider_registry,
        )
        capability_key = audit_capability_key(project)
        for provider_name in allowed_providers:
            if (
                provider_registry.is_available(provider_name)
                and not provider_registry.is_capability_limited(provider_name, capability_key)
            ):
                return SchedulingDecision(project=project, provider=provider_name)

    return None


def audit_capability_key(project: ProjectRecord) -> Optional[str]:
    """Stable audit task-family key used by the provider capability ledger."""
    preparation = (project.extra_data or {}).get("inbox_preparation")
    scope = preparation.get("scope") if isinstance(preparation, dict) else None
    scope = str(scope or project.name).strip().casefold()
    project_key = str(project.project_key or "unknown").strip().casefold()
    if not scope:
        return None
    return f"audit:{project_key}:{scope}"
