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


def _default_dod(project: ProjectRecord) -> list:
    items = [project.next_step] if project.next_step else []
    items.extend(project.open_feedback)
    return items or [project.main_task]


def build_orchestrator_task(
    project: ProjectRecord,
    definition_of_done: Optional[list] = None,
    provider: Optional[str] = None,
) -> OrchestratorTask:
    """Build the task handed to the autonomous orchestrator: the prepared
    work, an explicit DoD it must satisfy, and the checkpoint to resume
    from (empty for a fresh run)."""
    dod = list(definition_of_done) if definition_of_done else _default_dod(project)
    return OrchestratorTask(
        project_name=project.name,
        mode="autonomous",
        task=project.orchestrator_ready_task or project.main_task,
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
