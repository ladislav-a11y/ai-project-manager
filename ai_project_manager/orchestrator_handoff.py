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

from dataclasses import dataclass
from typing import Callable, Optional

from .models import ProjectRecord

# Below this length an orchestrator_ready_task is considered small enough
# to not necessarily need a full autonomous handoff; at/above it, or with
# a checkpoint already in progress, always hand off.
DEFAULT_HANDOFF_THRESHOLD_CHARS = 200


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

    def to_dict(self) -> dict:
        return {
            "project_name": self.project_name,
            "mode": self.mode,
            "task": self.task,
            "definition_of_done": list(self.definition_of_done),
            "checkpoint": dict(self.checkpoint),
            "provider": self.provider,
        }


def _goal_text(project: ProjectRecord) -> str:
    """Build the concrete goal handed to ai-orchestrator.

    Prefers an already-prepared ``orchestrator_ready_task``. Otherwise
    assembles one from the Trello card's main task, open feedback/bugs
    and next step, so a project is never handed off with an empty goal
    just because nobody filled in ``orchestrator_ready_task`` yet."""
    if project.orchestrator_ready_task and project.orchestrator_ready_task.strip():
        return project.orchestrator_ready_task.strip()

    parts = []
    if project.main_task and project.main_task.strip():
        parts.append(project.main_task.strip())
    open_feedback = [f.strip() for f in project.open_feedback if f and f.strip()]
    if open_feedback:
        parts.append("Open feedback/bugs:\n" + "\n".join(f"- {item}" for item in open_feedback))
    if project.next_step and project.next_step.strip():
        parts.append(f"Next step: {project.next_step.strip()}")
    return "\n\n".join(parts)


def _default_dod(project: ProjectRecord, goal_text: str) -> list:
    """Concrete DoD requirements pulled from the Trello card: the main
    task, the next step and every open feedback/bug item - never a
    blank placeholder. Falls back to the resolved goal text itself only
    when none of those are set, so a project with just a prepared
    ``orchestrator_ready_task`` still gets a non-empty DoD."""
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
    dod = list(definition_of_done) if definition_of_done else _default_dod(project, task_text)
    _validate_task(task_text, dod)
    return OrchestratorTask(
        project_name=project.name,
        mode="autonomous",
        task=task_text,
        definition_of_done=dod,
        checkpoint=dict(project.checkpoint),
        provider=provider or project.provider,
    )


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
