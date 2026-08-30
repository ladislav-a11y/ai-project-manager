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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from .guard import OrchestratorGuard
from .inbox import process_inbox
from .lock import ProjectLockManager
from .providers import ProviderRegistry, ProviderState
from .provider_state import save_provider_state
from .orchestrator_runner import (
    _PRIORITY_PREFIX_RE, ProjectPathError, resolve_project_path,
)
from .recovery import DEFAULT_MAX_ATTEMPTS, default_backoff, scan_for_recovery
from .runner import (
    DEFAULT_HOLDER,
    AuditRunFn,
    RunFn,
    RunOutcome,
    run_once,
    run_once_audit,
)
from .models import ProjectStatus
from .self_update import (
    check_self_update as check_self_update_fn,
    compute_code_version,
    package_root,
    prepare_safe_restart,
)
from .trello_sync import build_list_maps, fetch_all_projects, maintain_board_contract, sync_project_to_trello
from .slack_notify import notify

logger = logging.getLogger("ai_project_manager")

SleepFn = Callable[[float], None]
ProbeFn = Callable[[], bool]


def _seconds_until_next_tick(
    provider_registry: ProviderRegistry,
    poll_interval_seconds: float,
) -> float:
    """Return the shorter of the normal poll and the next provider retry.

    A long polling interval must not delay automatic recovery after an exact
    ``retry_after`` deadline.  Conversely, provider deadlines never extend the
    configured poll, because Trello may contain newly schedulable work.
    """
    delay = max(0.0, poll_interval_seconds)
    now = datetime.now(timezone.utc)
    provider_clock = getattr(provider_registry, "_clock", None)
    if callable(provider_clock):
        now = provider_clock()

    # Custom clocks are useful for embedding and tests, and older callers may
    # still return a naive UTC datetime.  Persisted retry deadlines are
    # normalized to aware UTC, so normalize the clock as well before doing
    # arithmetic; mixing naive and aware datetimes raises TypeError and would
    # terminate the unattended loop just when it is waiting for recovery.
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    for name in provider_registry.registered_names():
        status = provider_registry.get_status(name)
        if status.state == ProviderState.AVAILABLE or status.retry_after is None:
            continue
        retry_after = status.retry_after
        if retry_after.tzinfo is None and now.tzinfo is not None:
            retry_after = retry_after.replace(tzinfo=timezone.utc)
        delay = min(delay, max(0.0, (retry_after - now).total_seconds()))
    return delay


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


def _resume_due_provider_waits(client, projects: list, provider_registry: ProviderRegistry) -> list:
    """Resume due provider-limit waits in their owning workflow phase.

    A provider limit is represented by the physical ``Čeká na AI`` list,
    not by an active ``Pracuje se`` card.  Only waits with an explicit
    ``retry_after`` and a currently available provider are resumed; human
    holds and other paused cards remain untouched.  Audit waits return to
    ``Testování``; ordinary implementation waits return to ``Připraveno``.
    The existing checkpoint is deliberately preserved for the next tick.
    """
    now = _registry_now(provider_registry)
    resumed = []
    for project in projects:
        if project.status != ProjectStatus.PAUSED or not project.retry_after:
            continue
        try:
            retry_after = datetime.fromisoformat(project.retry_after)
        except (TypeError, ValueError):
            continue
        if retry_after.tzinfo is None:
            retry_after = retry_after.replace(tzinfo=timezone.utc)
        if now < retry_after:
            continue
        provider = project.provider
        if not provider or not provider_registry.is_available(provider):
            continue

        resume_status = project.extra_data.pop("resume_status", ProjectStatus.READY.value)
        if resume_status not in {
            ProjectStatus.READY.value,
            ProjectStatus.IN_PROGRESS.value,
            ProjectStatus.TESTING.value,
        }:
            resume_status = ProjectStatus.READY.value
        project.transition_to(ProjectStatus(resume_status))
        project.retry_after = None
        project.review_at = None
        sync_project_to_trello(client, project)
        resumed.append(project.name)
        notify(
            f"[AI Project Manager] Čekání skončilo: {project.name} - "
            f"karta vrácena do {'Testování' if resume_status == ProjectStatus.TESTING.value else 'Připraveno'} "
            "k pokračování z checkpointu."
        )
    return resumed


def _promote_completed_implementations_to_testing(client, projects: list) -> list[str]:
    """Expose the audit gate before invoking ai-orchestrator."""
    promoted = []
    for project in projects:
        if project.status != ProjectStatus.IN_PROGRESS:
            continue
        # A rejected audit may leave the prior implementation DoD checked;
        # its feedback must be handled as implementation work first.
        if project.returned_from_testing or not project.dod:
            continue
        implementation_items = [item for item in project.dod if item.phase == "implementation"]
        if not implementation_items or not all(item.checked for item in implementation_items):
            continue
        project.transition_to(ProjectStatus.TESTING)
        project.stop_reason = "implementation DoD complete; awaiting ai-orchestrator audit"
        sync_project_to_trello(client, project)
        promoted.append(project.name)
        logger.info(
            "promoted completed implementation to Testování before audit: project=%r",
            project.name,
        )
    return promoted


def _registry_now(provider_registry: ProviderRegistry) -> datetime:
    clock = getattr(provider_registry, "_clock", None)
    now = clock() if callable(clock) else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now


def _card_url_suffix(project) -> str:
    return f" ({project.trello_card_url})" if project.trello_card_url else ""


def _run_recovery_pass(
    client,
    projects: list,
    provider_registry: ProviderRegistry,
    max_attempts: int,
    backoff,
) -> list:
    """Revisit every currently-blocked project (see recovery.scan_for_recovery),
    persisting and logging whatever it decided - requeued (unblocked,
    checkpoint/priority preserved) or left BLOCKED with a concrete,
    actionable reason for a human. Projects not yet due for another look
    (their own backoff ``review_at``) are skipped without any write.

    Human-required Slack notifications are deduplicated: ``project.
    human_notified_reason`` records the exact reason text already reported,
    and a repeat classification with the *same* reason (the normal case
    while the backoff review keeps coming due and nothing has changed) is
    persisted but never re-sent to Slack - only a genuinely new reason, or
    the block clearing, produces another message. See the resume pass
    below for the "one Slack message when the blockage is lifted" half of
    that contract - it also covers a human fixing the card directly on the
    Trello board, which never produces a "requeued" outcome here at all.
    """
    outcomes = scan_for_recovery(projects, _registry_now(provider_registry), max_attempts=max_attempts, backoff=backoff)
    projects_by_card_id = {
        p.trello_card_id: p for p in projects if p.trello_card_id is not None
    }
    for outcome in outcomes:
        if outcome.action == "deferred":
            continue
        project = projects_by_card_id.get(outcome.trello_card_id)
        if project is None:
            # Unit/in-memory callers may construct records without a Trello
            # ID. Fall back only when the display name is actually unique.
            name_matches = [p for p in projects if p.name == outcome.project_name]
            project = name_matches[0] if len(name_matches) == 1 else None
        if project is None:
            logger.error(
                "recovery: cannot uniquely identify project=%r card_id=%r; skipping persistence",
                outcome.project_name, outcome.trello_card_id,
            )
            continue

        if outcome.action == "human_required":
            already_notified = (
                project.human_notified_reason == outcome.reason
                and project.human_action_step == outcome.step
            )
            project.human_notified_reason = outcome.reason
            project.human_action_step = outcome.step

        sync_project_to_trello(client, project)

        if outcome.action == "requeued":
            logger.info(
                "recovery: requeued project=%r cause=%s attempts=%d reason=%s",
                outcome.project_name, outcome.cause.value if outcome.cause else None,
                outcome.attempts, outcome.reason,
            )
            # If this project was previously flagged human-required, the
            # resume pass below sends the single "processing continues"
            # message instead - never both for the same transition.
            if not project.human_notified_reason:
                notify(f"[AI Project Manager] Auto-recovery: {outcome.project_name} znovu zařazeno - {outcome.reason}")
        else:
            logger.info(
                "recovery: human required project=%r cause=%s reason=%s",
                outcome.project_name, outcome.cause.value if outcome.cause else None, outcome.reason,
            )
            if not already_notified:
                notify(
                    f"[AI Project Manager] Vyžaduje zásah člověka: {project.name}{_card_url_suffix(project)}\n"
                    f"Důvod: {outcome.reason}\n"
                    f"Krok: {outcome.step}"
                )

    # Resume-after-human-intervention pass: fires exactly once per
    # transition out of a human-required block, whether that happened via
    # the auto-recovery requeue above or because a human fixed/unblocked
    # the card directly on the Trello board (which never produces a
    # "requeued" outcome from scan_for_recovery at all, since the project
    # is simply no longer blocked the next time it is fetched).
    for project in projects:
        if project.human_notified_reason and not project.is_blocked:
            project.human_notified_reason = None
            project.human_action_step = None
            sync_project_to_trello(client, project)
            notify(
                f"[AI Project Manager] Pokračuji: {project.name}{_card_url_suffix(project)} - "
                "blokace odstraněna, zpracování pokračuje."
            )

    return outcomes


def _bootstrap_project_keys(
    client,
    projects: list,
    project_paths: Optional[dict] = None,
    card_project_keys: Optional[dict] = None,
) -> None:
    """Assign missing identities only from an explicit card migration map.

    Root cause of the real production failure: ``project_key_from_labels``
    can only ever read a label that is already on the card, and the real
    board's pre-existing cards (created before the project_key label
    existed) carry only P0-P5 priority labels - never a project identity
    one. A content-phrase heuristic alone cannot fix that in general: a
    terse operational card like "P5 - Izolace testovacich Slack
    notifikaci" contains no trace of "AI Project Manager" anywhere in its
    title or task text, so there is no signal left to infer identity from.

    The sole migration source is ``card_project_keys``
    (``AI_PM_CARD_PROJECT_KEYS``): an explicit,
         one-time migration mapping keyed by either this exact Trello
         card's immutable ID, or (since an operator preparing this map
         from the board UI/a task description only ever sees the card's
         current title, never its internal ID) its exact current title.
         Both are exact-equality lookups, never a substring/phrase scan;
         title lookup is applied only when exactly one loaded card has that
         title, so duplicate display names can never cross-map cards. It is
         only ever
         applied, and only when it names one of the already configured
         stable identities (``project_paths``), so a typo can never
         fabricate a bogus label or point a card at the wrong repo. Once
         applied the card's ``project_key`` label is what persists the
         identity from then on - a later title edit can never undo it.
    No identity is inferred from content, title phrases, paths, or slugs.
    """
    project_paths = project_paths or {}
    card_project_keys = card_project_keys or {}
    stable_keys = [
        key for key in project_paths
        if key and not _PRIORITY_PREFIX_RE.match(key)
    ]
    title_counts: dict[str, int] = {}
    for project in projects:
        title_counts[project.name] = title_counts.get(project.name, 0) + 1

    for project in projects:
        if project.project_key:
            continue

        override = card_project_keys.get(project.trello_card_id)
        if not override and title_counts.get(project.name) == 1:
            override = card_project_keys.get(project.name)
        if override and override in stable_keys:
            project.project_key = override
            sync_project_to_trello(client, project)


def load_projects_and_inbox(
    client,
    inbox_list_name: str = "Inbox",
    default_priority: int = 2,
    project_paths: Optional[dict] = None,
    card_project_keys: Optional[dict] = None,
    process_inbox_enabled: bool = False,
) -> list:
    """Pull workflow project records from Trello.

    Inbox intake is deliberately opt-in while its lifecycle is incomplete;
    production PM scheduling only sees governed workflow lists.
    """
    projects = fetch_all_projects(client, exclude_list_names=(inbox_list_name,))
    _bootstrap_project_keys(
        client, projects, project_paths=project_paths, card_project_keys=card_project_keys
    )

    if not process_inbox_enabled:
        return projects

    def persist_inbox_project(project):
        card = sync_project_to_trello(client, project)
        # New Inbox classifications have no card identity until their first
        # sync. Retain it immediately so the rest of this tick can use the
        # same immutable identity as pre-existing cards.
        if project.trello_card_id is None:
            project.trello_card_id = card.get("id")
        return card

    changed = process_inbox(
        client,
        projects,
        inbox_list_name=inbox_list_name,
        default_priority=default_priority,
        persist_project=persist_inbox_project,
    )
    known_card_ids = {p.trello_card_id for p in projects if p.trello_card_id is not None}
    for project in changed:
        # Existing classifications are mutated in place by process_inbox.
        # Append only genuinely new cards, without ever deduplicating by the
        # editable/display-only card title.
        if project.trello_card_id not in known_card_ids:
            projects.append(project)
            if project.trello_card_id is not None:
                known_card_ids.add(project.trello_card_id)

    return projects


def _fail_closed_invalid_project_identities(client, projects: list, project_paths: dict) -> None:
    """Block unsafe work before runner selection can call the dispatcher."""
    for project in projects:
        if project.status not in {
            ProjectStatus.READY, ProjectStatus.IN_PROGRESS, ProjectStatus.TESTING
        }:
            continue
        try:
            repo_path = resolve_project_path(project, project_paths=project_paths)
            if not Path(repo_path).is_dir():
                raise ProjectPathError(
                    f"configured repository path does not exist or is not a directory: {repo_path}"
                )
        except ProjectPathError as exc:
            reason = f"project identity validation failed before dispatch: {exc}"
            project.stop_reason = reason
            project.blocked_by = reason
            project.transition_to(ProjectStatus.BLOCKED)
            sync_project_to_trello(client, project)
            notify(
                f"[AI Project Manager] Dispatch odmítnut: {project.name}{_card_url_suffix(project)}\n"
                f"Důvod: {reason}\nKrok: přidejte právě jeden známý projektový štítek "
                "a opravte jeho AI_PM_PROJECT_PATHS cestu."
            )


def run_tick(
    client,
    provider_registry: ProviderRegistry,
    run_fn: RunFn,
    holder: str = DEFAULT_HOLDER,
    providers_for_project: Optional[dict] = None,
    default_providers: Optional[list] = None,
    inbox_list_name: str = "Inbox",
    process_inbox_enabled: bool = False,
    lock_manager: Optional[ProjectLockManager] = None,
    probe: ProbeFn = _default_probe,
    provider_state_path: str = "provider_state.json",
    guard: Optional[OrchestratorGuard] = None,
    project_paths: Optional[dict] = None,
    card_project_keys: Optional[dict] = None,
    recovery_max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    recovery_backoff: Callable[[int], timedelta] = default_backoff,
    audit_run_fn: Optional[AuditRunFn] = None,
) -> RunOutcome:
    """Run exactly one scheduler tick: recheck due providers, load real
    Trello state, revisit any blocked project that is due for an
    unattended recovery pass (see recovery.py - independent of the normal
    priority scheduler, which never picks a blocked project at all), and
    run at most one project. Never spends an AI token when there is
    nothing schedulable.

    A project sitting in Testování is never eligible for the normal
    implementation dispatch below (see scheduler.NOT_SCHEDULABLE_STATUSES)
    - when ``audit_run_fn`` is configured, this tick tries the audit-only
    path first (``runner.run_once_audit``) so a card already awaiting an
    ai-orchestrator verdict is not starved by unrelated implementation
    priority; only when no audit work is currently pending does this tick
    fall through to the normal ``run_once`` dispatch. Without
    ``audit_run_fn`` (e.g. an embedder that has not wired up the audit
    path yet) a Testování card simply stays put - never falling back into
    ordinary implementation dispatch either way.
    """
    # Provider availability may change before a later Trello operation
    # fails (most importantly, run_fn can mark a provider LIMITED and the
    # subsequent card sync can fail). Persist in ``finally`` so a transient
    # board outage cannot lose retry_after/checkpoint state and cause an
    # immediate provider retry after process restart.
    try:
        # notify("AI Project Manager scheduler tick")
        recheck_due_providers(provider_registry, probe=probe)

        projects = load_projects_and_inbox(
            client,
            inbox_list_name=inbox_list_name,
            process_inbox_enabled=process_inbox_enabled,
            project_paths=project_paths,
            card_project_keys=card_project_keys,
        )
        # ``run_tick`` is also a generic scheduler primitive used with
        # non-repository run functions. The production CLI supplies the
        # explicit allowlist; only that dispatch mode owns repository
        # identity validation.
        if project_paths is not None:
            _fail_closed_invalid_project_identities(client, projects, project_paths)

        contract_issues = maintain_board_contract(client)
        for issue in contract_issues:
            notify(f"[AI Project Manager] Trello Card Contract vyžaduje zásah: {issue}")

        _resume_due_provider_waits(client, projects, provider_registry)

        _run_recovery_pass(
            client,
            projects,
            provider_registry,
            max_attempts=recovery_max_attempts,
            backoff=recovery_backoff,
        )

        # Reflect the test/audit phase on Trello before the audit provider is
        # invoked. This local transition spends no AI tokens.
        _promote_completed_implementations_to_testing(client, projects)

        outcome = None
        if audit_run_fn is not None:
            outcome = run_once_audit(
                client,
                projects,
                provider_registry,
                audit_run_fn,
                lock_manager=lock_manager,
                guard=guard,
                holder=holder,
                providers_for_project=providers_for_project,
                default_providers=default_providers,
            )

        if outcome is None or not outcome.ran:
            outcome = run_once(
                client,
                projects,
                provider_registry,
                run_fn,
                lock_manager=lock_manager,
                guard=guard,
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

        return outcome
    finally:
        save_provider_state(provider_state_path, provider_registry)


def run_maintenance_only(client) -> list[str]:
    """One-shot live Trello Card Contract migration/cleanup, independent of
    the scheduler - which stays on HOLD (see the fixed governance
    invariant): this never calls ``run_once``/dispatches a project to the
    orchestrator and never touches provider state.

    Migrates every safe card to the current schema, enforces governance
    and card identity, and reorders every list (see
    ``trello_sync.maintain_board_contract``). Every unsafe card is
    reported the same visible way as a normal tick (see ``run_tick``),
    plus a final Slack summary so a human has explicit, verifiable proof
    the pass ran, how many cards it covered and what it found - the
    result is checkable directly on the Trello board and in Slack,
    without needing to trust this process's own logs.
    """
    issues = maintain_board_contract(client)
    for issue in issues:
        notify(f"[AI Project Manager] Trello Card Contract vyžaduje zásah: {issue}")
    id_to_name, _ = build_list_maps(client)
    card_count = sum(len(client.list_cards(list_id)) for list_id in id_to_name)
    notify(
        "[AI Project Manager] Živá údržba Trello Card Contract dokončena: "
        f"{card_count} karet zkontrolováno, {len(issues)} problém(ů) vyžaduje zásah."
    )
    return issues


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
    process_inbox_enabled: bool = False,
    lock_manager: Optional[ProjectLockManager] = None,
    probe: ProbeFn = _default_probe,
    max_iterations: Optional[int] = None,
    provider_state_path: str = "provider_state.json",
    guard: Optional[OrchestratorGuard] = None,
    project_paths: Optional[dict] = None,
    card_project_keys: Optional[dict] = None,
    recovery_max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    recovery_backoff: Callable[[int], timedelta] = default_backoff,
    audit_run_fn: Optional[AuditRunFn] = None,
    check_self_update: bool = True,
    self_update_code_root: Optional[str] = None,
    self_update_repo_root: Optional[str] = None,
    self_update_started_version: Optional[str] = None,
    self_update_test_command: Optional[Sequence[str]] = None,
    self_update_run_tests=None,
    self_update_run_git=None,
) -> RunOutcome:
    """Run the scheduler forever (or, with ``once=True``, exactly one tick
    and return - the safe live-smoke-test mode). Sleeps between ticks
    only when a tick found no work, and that sleep never involves an AI
    call - it is a plain wait for the next poll.

    When ``once`` is False this is the one long-running process whose own
    already-imported code can go stale after ``ai-orchestrator`` edits this
    package's source files (a ``--once`` invocation never has this problem -
    it is a brand-new process every tick and always re-imports current
    code). After every tick this loop re-fingerprints its own source
    (``check_self_update``); on a detected change it verifies it is safe to
    hand off (regression tests + a resolvable, non-conflicted Git
    checkpoint - see ``self_update.prepare_safe_restart``), flushes
    persistent state, and returns with ``restart_required=True`` instead of
    looping again. It never restarts itself - ``cli.main`` exits with
    ``self_update.RESTART_REQUIRED_EXIT_CODE`` and only the separate
    supervising ``watchdog.py`` process actually launches the new one, so
    the replacement process is guaranteed a fresh interpreter/import state.
    If the safety check fails (tests red, or a broken working tree), the
    restart is deferred and this loop keeps running - no in-flight task is
    ever abandoned - but stops picking up *new* work every subsequent tick
    (``self_update_pending`` below) until the update either becomes safe to
    hand off or the on-disk change goes away, instead of repeatedly
    dispatching against code already known to be broken or unverifiable.
    """
    lock_manager = lock_manager or ProjectLockManager()
    # Created once and reused for every tick - never a fresh one per
    # tick - so a project failing with the exact same signature on
    # consecutive ticks is actually counted and eventually halted
    # instead of being retried forever (see guard.OrchestratorGuard).
    guard = guard or OrchestratorGuard()
    iterations = 0
    outcome = RunOutcome(ran=False, reason="not started")

    code_root = self_update_code_root or package_root()
    repo_root = self_update_repo_root or str(Path(code_root).resolve().parent)
    started_version = self_update_started_version
    if check_self_update and not once and started_version is None:
        started_version = compute_code_version(code_root)

    # Once a self-update has been detected but deferred (tests red / no
    # resolvable Git checkpoint), further ticks stop dispatching *new* work
    # on the known-stale, unverified code until the update either becomes
    # safe to hand off (checked every tick below) or the on-disk change goes
    # away. This is what actually satisfies "no duplicate dispatch": without
    # it, every poll would keep starting/continuing project work on code the
    # safety check has already flagged as broken or unverifiable, right up
    # to the moment a restart happens underneath it.
    self_update_pending = False

    while True:
        if self_update_pending:
            outcome = RunOutcome(
                ran=False,
                reason="self-update pending restart approval; deferring further dispatch",
            )
        else:
            try:
                outcome = run_tick(
                    client,
                    provider_registry,
                    run_fn,
                    holder=holder,
                    providers_for_project=providers_for_project,
                    default_providers=default_providers,
                    inbox_list_name=inbox_list_name,
                    process_inbox_enabled=process_inbox_enabled,
                    lock_manager=lock_manager,
                    probe=probe,
                    provider_state_path=provider_state_path,
                    guard=guard,
                    project_paths=project_paths,
                    card_project_keys=card_project_keys,
                    recovery_max_attempts=recovery_max_attempts,
                    recovery_backoff=recovery_backoff,
                    audit_run_fn=audit_run_fn,
                )
            except Exception as exc:  # noqa: BLE001 - keep the unattended daemon alive
                # Trello and provider-state persistence are external I/O. A
                # transient failure in either must fail this tick, not kill the
                # long-running scheduler process. The next iteration retries the
                # complete tick after the normal polling interval.
                logger.exception("scheduler tick failed; will retry after poll interval: %s", exc)
                outcome = RunOutcome(
                    ran=False,
                    reason=f"scheduler tick failed: {exc}",
                    operational_error=True,
                )
        iterations += 1
        logger.info(
            "scheduler tick finished: iteration=%d ran=%s reason=%s",
            iterations,
            outcome.ran,
            outcome.reason,
        )

        if check_self_update and not once and started_version is not None:
            status = check_self_update_fn(started_version, root=code_root)
            if status.changed:
                logger.warning(
                    "self-update detected (code changed since process start); "
                    "verifying it is safe to hand off for restart"
                )
                readiness = prepare_safe_restart(
                    repo_root,
                    persist_state=lambda: save_provider_state(provider_state_path, provider_registry),
                    **({"test_command": self_update_test_command} if self_update_test_command else {}),
                    **({"run_tests": self_update_run_tests} if self_update_run_tests else {}),
                    **({"run_git": self_update_run_git} if self_update_run_git else {}),
                )
                if readiness.safe:
                    logger.warning(
                        "self-update verified safe, requesting restart via supervising watchdog: %s",
                        readiness.reason,
                    )
                    outcome.restart_required = True
                    outcome.reason = f"self-update restart required: {readiness.reason}"
                    return outcome
                logger.warning(
                    "self-update detected but restart deferred (continuing on current code): %s",
                    readiness.reason,
                )
                self_update_pending = True
            else:
                self_update_pending = False

        if once:
            return outcome
        if max_iterations is not None and iterations >= max_iterations:
            return outcome

        if not outcome.ran:
            sleep_seconds = _seconds_until_next_tick(
                provider_registry,
                poll_interval_seconds,
            )
            logger.info("sleeping %.0fs until next tick", sleep_seconds)
            sleep(sleep_seconds)
