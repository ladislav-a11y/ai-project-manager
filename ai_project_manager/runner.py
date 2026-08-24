"""One scheduler tick, end to end: pick a project, lock it, run it,
sync the outcome back to Trello.

Picking a project and provider (``pick_next_project``) is pure local
logic - dict lookups and comparisons - so when there is nothing
schedulable, ``run_once`` returns immediately without acquiring a lock,
calling ``run_fn`` or touching Trello. No AI token is ever spent just to
find out there is no work. Only once real work is found does this
module acquire the project lock (released via ``ProjectLockManager.hold``
even if ``run_fn`` raises), invoke the run, and then unconditionally
sync the resulting state - status, provider, checkpoint, stop reason,
retry_after, last output and next step - back onto the Trello card.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from .guard import OrchestratorGuard
from .lock import ProjectLockError, ProjectLockManager
from .models import ProjectRecord, ProjectStatus
from .providers import ProviderRegistry
from .scheduler import pick_next_project
from .trello_sync import sync_project_to_trello
from .slack_notify import notify

logger = logging.getLogger("ai_project_manager")

# run_fn performs the actual provider/orchestrator call for one project
# and returns a result dict with any of: checkpoint, last_output,
# next_step, stop_reason, retry_after, status.
RunFn = Callable[[ProjectRecord, str], dict]

DEFAULT_HOLDER = "project-manager"


@dataclass
class RunOutcome:
    ran: bool
    project_name: Optional[str] = None
    provider: Optional[str] = None
    reason: Optional[str] = None
    halted: bool = False


def _apply_run_result(project: ProjectRecord, result: dict) -> None:
    if "checkpoint" in result:
        project.checkpoint = dict(result["checkpoint"])
    if "last_output" in result:
        project.last_output = result["last_output"]
    if "next_step" in result:
        project.next_step = result["next_step"]
    if "stop_reason" in result:
        project.stop_reason = result["stop_reason"]
    if "retry_after" in result:
        project.retry_after = result["retry_after"]
    if "status" in result:
        status = result["status"]
        project.status = ProjectStatus(status) if isinstance(status, str) else status


def run_once(
    client,
    projects: list[ProjectRecord],
    provider_registry: ProviderRegistry,
    run_fn: RunFn,
    lock_manager: Optional[ProjectLockManager] = None,
    guard: Optional[OrchestratorGuard] = None,
    holder: str = DEFAULT_HOLDER,
    providers_for_project: Optional[dict] = None,
    default_providers: Optional[list] = None,
) -> RunOutcome:
    """Run exactly one project's worth of work, if any is schedulable.

    Returns a ``RunOutcome`` describing what happened - ``ran=False``
    means the scheduler found nothing to do and nothing else was
    touched (no lock, no run_fn call, no Trello write).
    """
    decision = pick_next_project(
        projects,
        provider_registry,
        providers_for_project=providers_for_project,
        default_providers=default_providers,
    )
    if decision is None:
        logger.info("no schedulable project with an available provider")
        return RunOutcome(ran=False, reason="no schedulable project with an available provider")

    project = decision.project
    provider = decision.provider
    lock_manager = lock_manager or ProjectLockManager()
    guard = guard or OrchestratorGuard()

    logger.info("selected project=%r provider=%s", project.name, provider)
    # notify("Selected project: " + project.name + " | Provider: " + provider)

    try:
        with lock_manager.hold(project.name, holder):
            project.provider = provider
            logger.info(
                "dispatching project=%r to provider=%s in autonomous mode (checkpoint=%s)",
                project.name, provider, project.checkpoint,
            )
            # notify("Dispatching: " + project.name + " | Provider: " + provider)
            try:
                result = run_fn(project, provider)
            except Exception as exc:  # noqa: BLE001 - run failures are reported on the card, not raised
                signature = str(exc)
                halted = guard.record_denial(project.name, signature)
                project.stop_reason = signature
                if halted:
                    project.status = ProjectStatus.BLOCKED
                    project.blocked_by = f"repeated failure: {signature}"
                logger.warning(
                    "run failed project=%r provider=%s error=%s halted=%s",
                    project.name, provider, signature, halted,
                )
                sync_project_to_trello(client, project)
                logger.info("synced project=%r state to trello (card=%s)", project.name, project.trello_card_id)
                notify("Provider error: " + project.name + " | " + signature)
                return RunOutcome(
                    ran=True,
                    project_name=project.name,
                    provider=provider,
                    reason=signature,
                    halted=halted,
                )

            guard.reset(project.name)
            _apply_run_result(project, result)
            logger.info(
                "run result project=%r provider=%s status=%s stop_reason=%s retry_after=%s",
                project.name, provider, project.status.value, project.stop_reason, project.retry_after,
            )
            sync_project_to_trello(client, project)
            logger.info("synced project=%r state to trello (card=%s)", project.name, project.trello_card_id)
            return RunOutcome(
                ran=True,
                project_name=project.name,
                provider=provider,
                reason=project.stop_reason,
            )
    except ProjectLockError as exc:
        logger.info("project=%r locked by another worker, skipping: %s", project.name, exc)
        return RunOutcome(ran=False, project_name=project.name, provider=provider, reason=str(exc))
