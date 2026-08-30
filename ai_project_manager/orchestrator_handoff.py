"""Handoff of longer implementation tasks to the autonomous ai-orchestrator.

The Project Manager itself never grinds through a multi-step
implementation inline - once a project has real prepared work queued up
(or an in-flight checkpoint to resume), it is handed to the
ai-orchestrator in "autonomous" mode together with an explicit
Definition of Done and the project's current checkpoint, so the
orchestrator can pick up exactly where the last run left off instead of
restarting from scratch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from .models import DoDItem, ProjectRecord, ProjectStatus
from .card_contract import GOVERNANCE_POLICY

# The two task modes ai-orchestrator accepts (see OrchestratorTask.mode /
# orchestrator_runner._render_spec_markdown's "--mode" argv). AUTONOMOUS
# hands off real implementation work; AUDIT hands off a Testování card for
# a read-only verification pass - it must never itself make code changes,
# only issue the accepted/rejected verdict this PM then relays verbatim
# (see apply_audit_verdict).
AUTONOMOUS_MODE = "autonomous"
AUDIT_MODE = "audit"

AUDIT_VERDICT_ACCEPTED = "accepted"
AUDIT_VERDICT_REJECTED = "rejected"
_VALID_AUDIT_VERDICTS = {AUDIT_VERDICT_ACCEPTED, AUDIT_VERDICT_REJECTED}

# Where a rejected audit may send the card back to - Pracuje se (more
# implementation work continues, e.g. a checkpoint exists) or Připraveno
# (start the next attempt fresh). Which one applies is decided by
# ai-orchestrator from the concrete rejection feedback, never guessed here.
_VALID_REJECT_TARGETS = {ProjectStatus.IN_PROGRESS, ProjectStatus.READY}

# Below this length an orchestrator_ready_task is considered small enough
# to not necessarily need a full autonomous handoff; at/above it, or with
# a checkpoint already in progress, always hand off.
DEFAULT_HANDOFF_THRESHOLD_CHARS = 200

_CHECKLIST_ITEM_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\[[ xX]\]\s+(.+?)\s*[,;]?\s*$"
)
_INLINE_CHECKLIST_ITEM_RE = re.compile(
    r"\[[ xX]\]\s*([^\[]+?)(?=\s*\[[ xX]\]|\s*$)"
)


def needs_orchestrator_handoff(
    project: ProjectRecord, threshold: int = DEFAULT_HANDOFF_THRESHOLD_CHARS
) -> bool:
    """A project needs a full autonomous-orchestrator handoff when it
    already has an in-progress checkpoint to resume, or it has a
    substantial amount of prepared work queued up."""
    if project.checkpoint:
        return True
    return len(project.orchestrator_ready_task or "") >= threshold


class InvalidTaskError(RuntimeError):
    """Raised when a project has no real work to hand to the autonomous
    orchestrator - no goal/task text at all, and/or a Definition of Done
    with no concrete requirement (e.g. an all-blank list). Starting an
    autonomous run in that state cannot do anything useful and only
    burns AI tokens, so this is checked and refused before any dispatch
    happens - see ``build_orchestrator_task``."""


@dataclass
class OrchestratorTask:
    """The payload handed to the autonomous orchestrator for one run."""

    project_name: str
    mode: str
    task: str
    definition_of_done: list
    checkpoint: dict
    provider: Optional[str] = None
    governance: dict = None

    def __post_init__(self) -> None:
        if self.governance is None:
            self.governance = dict(GOVERNANCE_POLICY)
        if self.governance != GOVERNANCE_POLICY:
            raise InvalidTaskError("orchestrator handoff violates the fixed governance policy")

    def to_dict(self) -> dict:
        return {
            "project_name": self.project_name,
            "mode": self.mode,
            "task": self.task,
            "definition_of_done": list(self.definition_of_done),
            "checkpoint": dict(self.checkpoint),
            "provider": self.provider,
            "governance": dict(self.governance),
        }


def _goal_text(project: ProjectRecord) -> str:
    """Build the concrete goal handed to ai-orchestrator.

    Prefers an already-prepared ``orchestrator_ready_task``. Otherwise
    assembles one from the Trello card's main task, open feedback/bugs
    and next step, so a project is never handed off with an empty goal
    just because nobody filled in ``orchestrator_ready_task`` yet."""
    parts = []
    has_prepared_task = bool(project.orchestrator_ready_task and project.orchestrator_ready_task.strip())
    if has_prepared_task:
        parts.append(project.orchestrator_ready_task.strip())
    elif project.main_task and project.main_task.strip():
        parts.append(project.main_task.strip())
    open_feedback = [f.strip() for f in project.open_feedback if f and f.strip()]
    has_continuity = project.returned_from_testing or bool(project.last_output and project.last_output.strip())
    if open_feedback and (not has_prepared_task or has_continuity):
        parts.append("Open feedback/bugs:\n" + "\n".join(f"- {item}" for item in open_feedback))
    if project.next_step and project.next_step.strip() and (not has_prepared_task or has_continuity):
        parts.append(f"Next step: {project.next_step.strip()}")
    if project.last_output and project.last_output.strip():
        # This is durable continuity from Trello, not a second source of
        # truth. Keep it bounded so a long prior result cannot inflate every
        # later provider prompt.
        previous = project.last_output.strip()
        if len(previous) > 2000:
            previous = previous[-2000:]
        parts.append(
            "Výsledek předchozího ověřeného běhu zapsaný v Trellu (použij ho "
            "ke změně strategie, neopakuj beze změny stejný postup):\n" + previous
        )
    return "\n\n".join(parts)


def _default_dod(project: ProjectRecord, goal_text: str) -> list:
    """Concrete DoD requirements pulled from the Trello card: the main
    task, the next step and every open feedback/bug item - never a
    blank placeholder. Falls back to the resolved goal text itself only
    when none of those are set, so a project with just a prepared
    ``orchestrator_ready_task`` still gets a non-empty DoD."""
    # A card's own Definition-of-Done checklist (see
    # trello_sync.project_from_card/_build_dod) is already the merged,
    # deduplicated, deterministically-ordered checklist - the visible
    # checklist above PM-DATA first, then any explicit structured items
    # not already covered by it. Once set, it is the single source of
    # truth for what gets handed to the orchestrator; re-deriving it from
    # raw task text here would risk silently dropping items again (the
    # exact bug this guards against).
    if project.dod:
        items = [item.text.strip() for item in project.dod if item.text and item.text.strip()]
        if items:
            return items

    checklist_items: list[str] = []
    checklist_sources = [
        project.orchestrator_ready_task,
        project.main_task,
        project.next_step,
        *project.open_feedback,
    ]
    for source in checklist_sources:
        for line in (source or "").splitlines():
            match = _CHECKLIST_ITEM_RE.match(line)
            if match and match.group(1).strip():
                checklist_items.append(match.group(1).strip())
                continue
            for inline_match in _INLINE_CHECKLIST_ITEM_RE.finditer(line):
                if inline_match.group(1).strip():
                    checklist_items.append(inline_match.group(1).strip())

    # A Trello card's explicit checklist/DEFINITION OF DONE is more precise
    # than treating the whole card description as one giant requirement.
    # Accept both Markdown "- [ ]" and Trello's observed bare "[ ]" form.
    items = checklist_items
    if items:
        return list(dict.fromkeys(items))

    items = []
    if project.main_task and project.main_task.strip():
        items.append(project.main_task.strip())
    if project.next_step and project.next_step.strip():
        items.append(project.next_step.strip())
    items.extend(f.strip() for f in project.open_feedback if f and f.strip())
    if not items and goal_text:
        items.append(goal_text)

    deduped: list = []
    seen: set = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def materialize_project_dod(project: ProjectRecord) -> None:
    """Persist the concrete DoD used for an implementation run on the
    project snapshot, including when the source card had no explicit
    checkbox list.

    This prevents an agent's bare ``done`` response from being treated as
    proof that an implicit task was tested. Such a response must still pass
    through the explicit Testování/audit stage.
    """
    if project.dod:
        return
    goal_text = _goal_text(project)
    project.dod = [DoDItem(text=text) for text in _default_dod(project, goal_text)]


def implementation_dod(project: ProjectRecord) -> list[DoDItem]:
    """The only checklist entries an implementation agent may receive."""
    return [item for item in project.dod if item.phase == "implementation"]


def _normalized_checkpoint(checkpoint: Optional[dict], dod_count: int) -> dict:
    """Return a checkpoint whose DoD indices match the current checklist.

    Trello cards can be edited between runs.  Keeping indices from an older,
    longer checklist makes the visible DoD and resumable checkpoint disagree.
    Preserve every other checkpoint field, but discard invalid, boolean,
    duplicate and out-of-range DoD indices.
    """
    normalized = dict(checkpoint or {})
    if "completed_dod_indices" not in normalized:
        return normalized
    indices = []
    for index in normalized.get("completed_dod_indices") or []:
        if (
            isinstance(index, int)
            and not isinstance(index, bool)
            and 0 <= index < dod_count
            and index not in indices
        ):
            indices.append(index)
    normalized["completed_dod_indices"] = indices
    return normalized


def apply_dod_progress(project: ProjectRecord, checkpoint: Optional[dict]) -> None:
    """Mark ``project.dod`` items checked per the orchestrator's reported
    ``completed_dod_indices`` checkpoint (indices into the exact list
    ``_default_dod`` handed off, see ``build_orchestrator_task``).

    Only ever adds checked items - an index missing from a later
    checkpoint never un-checks an item a previous run already verified,
    since ``completed_dod_indices`` accumulates across resumed runs.
    """
    if not checkpoint or "completed_dod_indices" not in checkpoint:
        return
    items = implementation_dod(project)
    normalized = _normalized_checkpoint(checkpoint, len(items))
    checkpoint["completed_dod_indices"] = normalized["completed_dod_indices"]
    for index in normalized["completed_dod_indices"]:
        items[index].checked = True


def dod_fully_verified(project: ProjectRecord) -> bool:
    """Whether every Definition-of-Done item on this project is confirmed
    checked. A project with no explicit checklist (``project.dod`` empty)
    is vacuously verified here - ``_validate_task`` already refuses to
    dispatch a project with no concrete DoD at all."""
    items = implementation_dod(project)
    return not items or all(item.checked for item in items)


def _validate_task(task_text: str, dod: list) -> None:
    if not (task_text or "").strip():
        raise InvalidTaskError(
            "project has no goal/task text (empty orchestrator_ready_task, main_task, "
            "open_feedback and next_step) - refusing to start an autonomous run"
        )
    real_items = [item for item in dod if isinstance(item, str) and item.strip()]
    if not real_items:
        raise InvalidTaskError(
            "definition_of_done has no concrete requirement (empty, or blank strings only) - "
            "refusing to start an autonomous run"
        )


def build_orchestrator_task(
    project: ProjectRecord,
    definition_of_done: Optional[list] = None,
    provider: Optional[str] = None,
) -> OrchestratorTask:
    """Build the task handed to the autonomous orchestrator: the prepared
    work, an explicit DoD it must satisfy, and the checkpoint to resume
    from (empty for a fresh run).

    Raises ``InvalidTaskError`` rather than building a task with an
    empty goal or an all-blank DoD - see ``_validate_task``.
    """
    task_text = _goal_text(project)
    if definition_of_done is None:
        materialize_project_dod(project)
    dod = (
        list(definition_of_done)
        if definition_of_done is not None
        else [item.text.strip() for item in implementation_dod(project) if item.text.strip()]
    )
    _validate_task(task_text, dod)
    checkpoint = _normalized_checkpoint(project.checkpoint, len(dod))
    if definition_of_done is None:
        project.checkpoint = checkpoint
    return OrchestratorTask(
        project_name=project.name,
        mode=AUTONOMOUS_MODE,
        task=task_text,
        definition_of_done=dod,
        checkpoint=checkpoint,
        provider=provider or project.provider,
    )


class AuditVerdictError(RuntimeError):
    """Raised when an audit result cannot be trusted or applied as-is.

    This is the sole enforcement point for the "audit authority is
    exclusively ai-orchestrator's" invariant: it is deliberately strict
    about what counts as a verdict (only the literal strings "accepted"/
    "rejected", only ever read from ai-orchestrator's own reported
    result - never inferred, defaulted or guessed by the Project Manager)
    and about a rejection always carrying a concrete reason, so neither
    the Project Manager nor an agent can ever manufacture a verdict of
    their own by calling this with anything else.
    """


def build_audit_task(project: ProjectRecord, provider: Optional[str] = None) -> OrchestratorTask:
    """Build the audit-only task handed to ai-orchestrator for a card
    sitting in Testování.

    Unlike ``build_orchestrator_task`` (autonomous implementation), this
    never starts or continues implementation work - it asks
    ai-orchestrator to verify the project's existing Definition of Done
    against the real, already-implemented state and return an explicit
    accepted/rejected verdict (see ``apply_audit_verdict``). The full DoD
    - including already-checked items - travels along so the audit can
    confirm the whole checklist, not just what is still unchecked.
    """
    task_text = _goal_text(project)
    materialize_project_dod(project)
    dod = [item.text.strip() for item in project.dod if item.text and item.text.strip()]
    _validate_task(task_text, dod)
    # ai-orchestrator's supported autonomous protocol enters its independent
    # test/audit phase only after the input checklist is complete. Mark this
    # audit copy complete so audit-only criteria never wake the executor; the
    # original ProjectRecord remains unchanged until an explicit verdict.
    audit_checkpoint = dict(project.checkpoint)
    audit_checkpoint["completed_dod_indices"] = list(range(len(dod)))
    return OrchestratorTask(
        project_name=project.name,
        mode=AUDIT_MODE,
        task=task_text,
        definition_of_done=dod,
        checkpoint=audit_checkpoint,
        provider=provider or project.provider,
    )


def apply_audit_verdict(
    project: ProjectRecord,
    verdict: str,
    reason: Optional[str] = None,
    evidence: Optional[str] = None,
    reject_target: Optional[ProjectStatus] = None,
) -> None:
    """Apply ai-orchestrator's audit verdict to a Testování project - the
    only place a Testování card's lifecycle may change.

    ``verdict`` must be exactly "accepted" or "rejected", as reported by
    ai-orchestrator itself; anything else (missing, blank, a status word,
    a guess) raises ``AuditVerdictError`` instead of silently doing
    nothing or picking a default - the Project Manager and the agent are
    never allowed to issue their own accepted/rejected decision.

    accepted: requires concrete audit evidence, every Definition of Done
    item is marked checked (the audit is itself the final DoD item's
    verification), and the card moves to Hotovo/DONE.

    rejected: requires concrete ``reason`` and ``evidence`` from the audit -
    refused otherwise, since a rejection with no actionable feedback or
    traceable evidence would leave a human or the next implementation run
    with nothing reliable to act on. The reason and evidence are recorded on
    the card's open feedback and stop reason,
    and the card returns to Pracuje se (default - there is more
    implementation work to continue) or, when ai-orchestrator explicitly
    says so via ``reject_target``, to Připraveno (start the next attempt
    fresh). The Definition of Done is left exactly as it was - a
    rejection never itself checks or unchecks an item.
    """
    if verdict not in _VALID_AUDIT_VERDICTS:
        raise AuditVerdictError(
            f"invalid audit verdict {verdict!r}; expected one of "
            f"{sorted(_VALID_AUDIT_VERDICTS)} as reported by ai-orchestrator"
        )

    if not evidence or not evidence.strip():
        raise AuditVerdictError(
            f"{verdict} audit verdict requires concrete evidence from ai-orchestrator - "
            "refusing to persist an untraceable audit result"
        )

    if verdict == AUDIT_VERDICT_ACCEPTED:
        from .dod_validator import _GENERIC_TRIVIAL_EVIDENCE_RE
        if _GENERIC_TRIVIAL_EVIDENCE_RE.match(evidence.strip()):
            raise AuditVerdictError(
                f"accepted audit verdict requires concrete evidence, but got trivial placeholder '{evidence.strip()}'"
            )
        for item in project.dod:
            item.checked = True
        # A successful audit closes the corrective loop. Leaving the return
        # marker on a Hotovo card would make completed work look like active
        # corrective work to later board-maintenance/prioritization passes.
        project.extra_data.pop("returned_from_testing", None)
        project.extra_data.pop("return_reason", None)
        project.stop_reason = None
        project.blocked_by = None
        project.last_output = evidence.strip()
        project.transition_to(ProjectStatus.DONE)
        return

    if not reason or not reason.strip():
        raise AuditVerdictError(
            "rejected audit verdict requires a concrete reason from ai-orchestrator - "
            "refusing to return a card to implementation with no actionable feedback"
        )
    target = reject_target if reject_target is not None else ProjectStatus.IN_PROGRESS
    if target not in _VALID_REJECT_TARGETS:
        raise AuditVerdictError(
            f"invalid audit reject_target {target!r}; expected one of {sorted(_VALID_REJECT_TARGETS, key=str)}"
        )

    feedback_entry = reason.strip()
    feedback_entry = f"{feedback_entry}\nEvidence: {evidence.strip()}"
    project.open_feedback = [*project.open_feedback, feedback_entry]
    project.stop_reason = reason.strip()
    if target == ProjectStatus.IN_PROGRESS:
        project.mark_returned_from_testing("audit_rejected")
    project.transition_to(target)


# A dispatcher performs the actual call out to the ai-orchestrator process
# and returns a result dict with any of: checkpoint, last_output,
# next_step, stop_reason.
DispatchFn = Callable[[OrchestratorTask], dict]


def dispatch_to_orchestrator(
    project: ProjectRecord,
    dispatch: DispatchFn,
    definition_of_done: Optional[list] = None,
    provider: Optional[str] = None,
) -> dict:
    """Hand a project off to the autonomous orchestrator and fold the
    result (new checkpoint / output / next step / stop reason) back onto
    the project record so a later run resumes from where this one left
    off."""
    task = build_orchestrator_task(project, definition_of_done=definition_of_done, provider=provider)
    result = dispatch(task)

    if "checkpoint" in result:
        project.checkpoint = dict(result["checkpoint"])
    if "last_output" in result:
        project.last_output = result["last_output"]
    if "next_step" in result:
        project.next_step = result["next_step"]
    if "stop_reason" in result:
        project.stop_reason = result["stop_reason"]
    return result
