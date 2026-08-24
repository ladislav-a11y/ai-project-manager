"""The persistent scheduler loop - the actual unattended ("bezobsluzny")
runtime.

Each tick: cheaply re-verify any provider past its ``retry_after``
(``recheck_due_providers`` - no AI call, see providers.py), pull real
project/Inbox state from Trello, and run at most one project's worth of
work via ``runner.run_once``. ``run_once`` itself is already a no-op
(no lock, no run_fn call, no Trello write) when nothing is schedulable,
so a tick with no work or with every provider LIMITED never spends an
AI token - it just logs and sleeps until the next poll.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .inbox import process_inbox
from .lock import ProjectLockManager
from .providers import ProviderRegistry, ProviderState
from .provider_state import save_provider_state
from .runner import DEFAULT_HOLDER, RunFn, RunOutcome, run_once
from .trello_sync import fetch_all_projects, sync_project_to_trello
from .slack_notify import notify

logger = logging.getLogger("ai_project_manager")

SleepFn = Callable[[float], None]
ProbeFn = Callable[[], bool]


def _default_probe() -> bool:
    """Cheap, free recheck: once retry_after has passed we simply allow
    the provider to be tried again. We never spend a dedicated AI/network
    call just to check - if it is still failing, the very next real
    run_fn call will hit that failure and re-limit it (see
    orchestrator_runner.build_run_fn)."""
    return True


def recheck_due_providers(provider_registry: ProviderRegistry, probe: ProbeFn = _default_probe) -> list:
    """Re-verify every provider whose retry_after has passed. Returns the
    names of providers that became AVAILABLE again this call, so a
    previously LIMITED project can resume automatically without any
    manual intervention."""
    resumed = []
    for name in provider_registry.registered_names():
        if provider_registry.is_due_for_recheck(name):
            status = provider_registry.recheck(name, probe)
            if status.state == ProviderState.AVAILABLE:
                logger.info("provider %s is available again, resuming (checkpoint=%s)", name, status.checkpoint)
                resumed.append(name)
    return resumed


def load_projects_and_inbox(client, inbox_list_name: str = "Inbox", default_priority: int = 2) -> list:
    """Pull real project records from Trello and fold in any new Inbox
    cards - the single manual input point - returning the up-to-date
    project list."""
    projects = fetch_all_projects(client, exclude_list_names=(inbox_list_name,))
    projects_by_name = {p.name: p for p in projects}

    changed = process_inbox(
        client,
        list(projects_by_name.values()),
        inbox_list_name=inbox_list_name,
        default_priority=default_priority,
    )
    for project in changed:
        sync_project_to_trello(client, project)
        projects_by_name[project.name] = project

    return list(projects_by_name.values())


def run_tick(
    client,
    provider_registry: ProviderRegistry,
    run_fn: RunFn,
    holder: str = DEFAULT_HOLDER,
    providers_for_project: Optional[dict] = None,
    default_providers: Optional[list] = None,
    inbox_list_name: str = "Inbox",
    lock_manager: Optional[ProjectLockManager] = None,
    probe: ProbeFn = _default_probe,
) -> RunOutcome:
    """Run exactly one scheduler tick: recheck due providers, load real
    Trello state, and run at most one project. Never spends an AI token
    when there is nothing schedulable."""
    # notify("AI Project Manager scheduler tick")
    recheck_due_providers(provider_registry, probe=probe)

    projects = load_projects_and_inbox(client, inbox_list_name=inbox_list_name)

    outcome = run_once(
        client,
        projects,
        provider_registry,
        run_fn,
        lock_manager=lock_manager,
        holder=holder,
        providers_for_project=providers_for_project,
        default_providers=default_providers,
    )

    if outcome.ran:
        logger.info(
            "ran project=%s provider=%s stop_reason=%s halted=%s",
            outcome.project_name, outcome.provider, outcome.reason, outcome.halted,
        )
    else:
        logger.info("no schedulable work this tick (%s)", outcome.reason)
        _log_providers_waiting_on_retry_after(provider_registry)

    save_provider_state("provider_state.json", provider_registry)

    return outcome


def _log_providers_waiting_on_retry_after(provider_registry: ProviderRegistry) -> None:
    """Log every provider that is currently gating scheduling because it
    is LIMITED/ERROR and not yet due for a recheck, so an operator
    watching the logs can see exactly what the loop is waiting on."""
    for name in provider_registry.registered_names():
        status = provider_registry.get_status(name)
        if status.state != ProviderState.AVAILABLE and status.retry_after is not None:
            logger.info(
                "provider %s is %s, waiting until retry_after=%s",
                name, status.state, status.retry_after.isoformat(),
            )


def run_loop(
    client,
    provider_registry: ProviderRegistry,
    run_fn: RunFn,
    once: bool = False,
    poll_interval_seconds: float = 300.0,
    sleep: SleepFn = time.sleep,
    holder: str = DEFAULT_HOLDER,
    providers_for_project: Optional[dict] = None,
    default_providers: Optional[list] = None,
    inbox_list_name: str = "Inbox",
    lock_manager: Optional[ProjectLockManager] = None,
    probe: ProbeFn = _default_probe,
    max_iterations: Optional[int] = None,
) -> RunOutcome:
    """Run the scheduler forever (or, with ``once=True``, exactly one tick
    and return - the safe live-smoke-test mode). Sleeps between ticks
    only when a tick found no work, and that sleep never involves an AI
    call - it is a plain wait for the next poll."""
    lock_manager = lock_manager or ProjectLockManager()
    iterations = 0
    outcome = RunOutcome(ran=False, reason="not started")

    while True:
        outcome = run_tick(
            client,
            provider_registry,
            run_fn,
            holder=holder,
            providers_for_project=providers_for_project,
            default_providers=default_providers,
            inbox_list_name=inbox_list_name,
            lock_manager=lock_manager,
            probe=probe,
        )
        iterations += 1

        if once:
            return outcome
        if max_iterations is not None and iterations >= max_iterations:
            return outcome

        logger.info("sleeping %.0fs until next tick", poll_interval_seconds)
        sleep(poll_interval_seconds)
