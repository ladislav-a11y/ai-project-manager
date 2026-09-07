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
from .artifact_cleanup import cleanup_test_artifacts
from .inbox import InboxPlannerFn, archive_completed_inbox_sources, process_inbox
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
    FinalizeFn,
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


def _resume_due_provider_waits(
    client,
    projects: list,
    provider_registry: ProviderRegistry,
    providers_for_project: Optional[dict] = None,
    default_providers: Optional[list[str]] = None,
) -> list:
    """Resume due provider-limit waits in their owning workflow phase.

    A provider limit is represented by the physical ``Čeká na AI`` list,
    not by an active ``Pracuje se`` card.  Only waits with an explicit
    ``retry_after`` and a currently available provider are resumed; when the
    recorded provider is still limited but another configured provider is
    available, the card fails over immediately on the next tick. Human holds
    and other paused cards remain untouched. Audit waits return to
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
        provider = project.provider
        allowed_providers = (providers_for_project or {}).get(
            project.name,
            default_providers or provider_registry.registered_names(),
        )
        fallback_provider = next(
            (
                candidate
                for candidate in allowed_providers
                if candidate != provider and provider_registry.is_available(candidate)
            ),
            None,
        )
        current_provider_available = bool(provider and provider_registry.is_available(provider))
        retry_due = now >= retry_after
        if not retry_due and fallback_provider is None:
            continue

        resume_status = project.extra_data.pop("resume_status", ProjectStatus.READY.value)
        if resume_status not in {
            ProjectStatus.READY.value,
            ProjectStatus.IN_PROGRESS.value,
            ProjectStatus.TESTING.value,
        }:
            resume_status = ProjectStatus.READY.value
        project.retry_after = None
        project.review_at = None
        if fallback_provider is not None and not current_provider_available:
            project.provider = fallback_provider
        project.transition_to(ProjectStatus(resume_status))
        project.stop_reason = (
            f"čekání na providera skončilo; pokračování z checkpointu přes "
            f"{project.provider or 'dostupného providera'}"
        )
        selection = project.extra_data.get("provider_selection")
        if isinstance(selection, dict):
            selection.update(
                provider=project.provider,
                selected_provider=project.provider,
                selected_model=None,
                model=None,
                actual_provider=None,
                actual_model=None,
                stage="audit" if resume_status == ProjectStatus.TESTING.value else "implementation",
                source="provider_default",
                provider_reason=(
                    f"provider {project.provider} pokračuje z checkpointu; "
                    "modelový override se znovu vyhodnotí podle fáze při dalším dispatchi"
                ),
            )
        sync_project_to_trello(client, project)
        resumed.append(project.name)
        destination = (
            "Testování" if resume_status == ProjectStatus.TESTING.value else "Připraveno"
        )
        if fallback_provider is not None and not current_provider_available:
            notify(
                f"[AI Project Manager] Failover: {project.name} - provider {provider or 'neznámý'} "
                f"není dostupný, karta vrácena do {destination} pro dostupný provider "
                f"{fallback_provider} z checkpointu."
            )
        else:
            notify(
                f"[AI Project Manager] Čekání skončilo: {project.name} - "
                f"karta vrácena do {destination} k pokračování z checkpointu."
            )
    return resumed


def _promote_completed_implementations_to_testing(
    client, projects: list, finalize_fn: Optional[FinalizeFn] = None
) -> list[str]:
    """Expose the audit gate before invoking ai-orchestrator.

    A plain implementation DoD item (the normal case - no explicit
    commit/push wording) never asks the controller finalizer to run by
    name (see ``orchestrator_runner._finalization_indices``), so without
    this step nothing would ever be committed and every independent audit
    would find an unchanged checkout - the "checkout se nezmenil" rejection
    loop this was written to fix. ``finalize_fn`` is called here, once,
    right before promotion; a card whose finalization fails stays in
    Pracuje se with a concrete reason instead of entering Testování with
    unfinalized (uncommitted) work. This spends no AI token either way -
    finalization is a deterministic commit/test/push subprocess, not a
    provider call.
    """
    promoted = []
    for project in projects:
        if project.status != ProjectStatus.IN_PROGRESS:
            continue
        # A stale return marker from an older audit must not force another
        # implementation dispatch once every implementation item is already
        # checked. A real implementation rejection is kept in this phase by
        # apply_audit_verdict reopening the rejected item.
        if not project.dod:
            continue
        implementation_items = [item for item in project.dod if item.phase == "implementation"]
        if not implementation_items or not all(item.checked for item in implementation_items):
            continue
        if finalize_fn is not None:
            finalize_result = finalize_fn(project) or {}
            if finalize_result.get("status") != "done":
                reason = finalize_result.get("stop_reason") or "controller finalization failed"
                project.stop_reason = (
                    "implementation DoD complete but controller finalization failed: "
                    f"{reason}"
                )
                sync_project_to_trello(client, project)
                logger.warning(
                    "controller finalization blocked promotion to Testování: "
                    "project=%r reason=%s",
                    project.name, reason,
                )
                continue
            if not finalize_result.get("already_verified"):
                project.checkpoint = finalize_result.get("checkpoint", project.checkpoint)
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
) -> list:
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
    migrated = []
    stable_keys = [
        key for key in project_paths
        if key and not _PRIORITY_PREFIX_RE.match(key)
    ]
    # ``ProjectRecord.name`` deliberately excludes the visible P<n> prefix,
    # but the one-time migration map may be keyed by the exact current
    # Trello title. Keep that raw title separately so title-keyed migrations
    # remain exact after priority prefixes became mandatory.
    title_counts: dict[str, int] = {}
    raw_titles: dict[str, str] = {}
    for project in projects:
        raw_title = project.name
        if project.trello_card_id:
            try:
                raw_title = str(client.get_card(project.trello_card_id).get("name") or project.name)
            except Exception:
                raw_title = project.name
        raw_titles[project.trello_card_id] = raw_title
        title_counts[raw_title] = title_counts.get(raw_title, 0) + 1

    for project in projects:
        preparation = (project.extra_data or {}).get("inbox_preparation")
        source_card_id = (
            preparation.get("source_card_id")
            if isinstance(preparation, dict)
            else None
        )
        override = card_project_keys.get(project.trello_card_id)
        if not override and source_card_id:
            override = card_project_keys.get(source_card_id)
        raw_title = raw_titles.get(project.trello_card_id, project.name)
        if not override and title_counts.get(raw_title) == 1:
            override = card_project_keys.get(raw_title)
        # An explicit migration may repair a stale generated identity, but
        # cards without a matching migration keep their persisted identity.
        if not override and project.project_key:
            continue
        if override and override in stable_keys:
            configured_path = project_paths.get(override)
            preparation_path = (
                preparation.get("project_path")
                if isinstance(preparation, dict)
                else None
            )
            generated_path_is_stale = bool(
                isinstance(preparation, dict)
                and preparation.get("generated_project")
                and configured_path
                and preparation_path
                and Path(str(configured_path)).expanduser().resolve()
                != Path(str(preparation_path)).expanduser().resolve()
            )
            identity_changed = project.project_key != override
            blocked_stale_path = bool(
                project.status == ProjectStatus.BLOCKED
                and generated_path_is_stale
                and not preparation.get("identity_migration")
            )
            if identity_changed:
                project.project_key = override
            if identity_changed or blocked_stale_path:
                if blocked_stale_path:
                    preparation["identity_migration"] = {
                        "from_project_path": preparation_path,
                        "to_project_key": override,
                        "reason": "stale generated checkout replaced by explicit stable checkout",
                    }
                    project.extra_data["inbox_preparation"] = preparation
                sync_project_to_trello(client, project)
                migrated.append(project)
    return migrated


def _register_generated_project_paths(
    projects: list,
    project_paths: Optional[dict],
    projects_root: Optional[str],
) -> None:
    """Rehydrate safe auto-created Inbox project mappings after restart."""
    if project_paths is None or not projects_root:
        return
    root = Path(projects_root).expanduser().resolve()
    for project in projects:
        preparation = (project.extra_data or {}).get("inbox_preparation")
        if not isinstance(preparation, dict) or not preparation.get("generated_project"):
            continue
        key = project.project_key
        raw_path = preparation.get("project_path")
        if not key or not raw_path:
            continue
        candidate = Path(str(raw_path)).expanduser().resolve()
        if candidate == root or root not in candidate.parents:
            continue
        configured = project_paths.get(key)
        if configured is None or Path(str(configured)).expanduser().resolve() == candidate:
            project_paths[key] = str(candidate)


def load_projects_and_inbox(
    client,
    inbox_list_name: str = "Inbox",
    default_priority: int = 2,
    project_paths: Optional[dict] = None,
    card_project_keys: Optional[dict] = None,
    process_inbox_enabled: bool = False,
    projects_root: Optional[str] = None,
    planner: Optional[InboxPlannerFn] = None,
) -> list:
    """Pull workflow project records from Trello.

    Inbox intake is deliberately opt-in while its lifecycle is incomplete;
    production PM scheduling only sees governed workflow lists.
    """
    projects = fetch_all_projects(client, exclude_list_names=(inbox_list_name,))
    _bootstrap_project_keys(
        client, projects, project_paths=project_paths, card_project_keys=card_project_keys
    )
    _register_generated_project_paths(projects, project_paths, projects_root)

    if not process_inbox_enabled:
        return projects

    archived_sources = archive_completed_inbox_sources(
        client, projects, inbox_list_name=inbox_list_name
    )
    if archived_sources:
        logger.info(
            "Inbox intake reconciliation archived completed sources: %s",
            ", ".join(archived_sources),
        )

    def persist_inbox_project(project):
        card = sync_project_to_trello(client, project)
        # New Inbox classifications have no card identity until their first
        # sync. Retain it immediately so the rest of this tick can use the
        # same immutable identity as pre-existing cards.
        if project.trello_card_id is None:
            project.trello_card_id = card.get("id")
            # Persist the generated identity immediately; this is required
            # for split Inbox tasks and makes every target card self-bound in
            # the versioned Trello contract.
            card = sync_project_to_trello(client, project)
        return card

    # Inbox is still inspected on every tick, but AI planning is a separate
    # admission step.  Existing governed work has phase precedence: while a
    # A card in Připraveno is read back as either NEW (freshly admitted) or
    # READY (resumed).  Both physical states must gate Inbox planning.  Do
    # not spend a planner call on a new Inbox project while any governed work
    # is already queued or active; the scheduler below will select the
    # dependency-ready card from Připraveno after the intake gate.
    intake_gate_statuses = {
        ProjectStatus.NEW,
        ProjectStatus.READY,
        ProjectStatus.IN_PROGRESS,
        ProjectStatus.TESTING,
        ProjectStatus.PAUSED,
        ProjectStatus.BLOCKED,
        ProjectStatus.ERROR,
    }
    if any(project.status in intake_gate_statuses for project in projects):
        id_to_name, name_to_id = build_list_maps(client)
        inbox_id = name_to_id.get(inbox_list_name)
        inbox_count = len(client.list_cards(inbox_id)) if inbox_id else 0
        logger.info(
            "Inbox intake checked: planner skipped because governed work is active; "
            "inbox_cards=%s",
            inbox_count,
        )
        return projects

    changed = process_inbox(
        client,
        projects,
        inbox_list_name=inbox_list_name,
        default_priority=default_priority,
        persist_project=persist_inbox_project,
        project_paths=project_paths,
        card_project_keys=card_project_keys,
        projects_root=projects_root,
        planner=planner,
    )
    if changed:
        # Emit one compact Slack event per source card. The intake provider is
        # persisted in each child Card Contract so this event names the AI
        # that actually planned the human request; retired providers must never appear.
        by_source: dict[str, list] = {}
        for project in changed:
            metadata = (project.extra_data or {}).get("inbox_preparation", {})
            source_id = str(metadata.get("source_card_id") or project.trello_card_id or "unknown")
            by_source.setdefault(source_id, []).append(project)
        for source_id, prepared in by_source.items():
            priorities = ", ".join(
                f"P{project.priority:g}" for project in sorted(prepared, key=lambda item: -item.priority)
            )
            providers = sorted({
                str((project.extra_data or {}).get("inbox_preparation", {}).get("intake_provider") or "local-deterministic")
                for project in prepared
            })
            models = sorted({
                str((project.extra_data or {}).get("inbox_preparation", {}).get("intake_model") or "provider receipt nevrátil model")
                for project in prepared
            })
            provider_reasons = sorted({
                str((project.extra_data or {}).get("inbox_preparation", {}).get("intake_provider_reason") or "neuveden")
                for project in prepared
            })
            model_reasons = sorted({
                str((project.extra_data or {}).get("inbox_preparation", {}).get("intake_model_reason") or "neuveden")
                for project in prepared
            })
            message = (
                "[AI Project Manager] Inbox intake: "
                f"intake_provider={','.join(providers)}; intake_model={','.join(models)}; "
                f"intake_provider_reason={' || '.join(provider_reasons)}; "
                f"intake_model_reason={' || '.join(model_reasons)}; "
                f"source_card_id={source_id}; prepared_tasks={len(prepared)}; priorities=[{priorities}]; "
                "worker_provider=not_selected_in_intake"
            )
            # Keep the exact compact event in the local log as well as Slack;
            # HTTP 200 alone does not make the provider/model observable.
            logger.info("Inbox intake notification: %s", message)
            notify(message)
    known_card_ids = {p.trello_card_id for p in projects if p.trello_card_id is not None}
    for project in changed:
        # Intake admission and implementation dispatch are separate phases.
        # Keep this marker in memory only: a card prepared during this tick
        # must remain in Připraveno until the next tick, otherwise the same
        # scheduler pass can immediately move a brand-new Inbox task to
        # Pracuje se and spend provider tokens before the user can inspect it.
        preparation = (project.extra_data or {}).get("inbox_preparation")
        if isinstance(preparation, dict) and preparation.get("source_card_id"):
            setattr(project, "_prepared_this_tick", True)
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
    projects_root: Optional[str] = None,
    recovery_max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    recovery_backoff: Callable[[int], timedelta] = default_backoff,
    audit_run_fn: Optional[AuditRunFn] = None,
    inbox_planner: Optional[InboxPlannerFn] = None,
    finalize_fn: Optional[FinalizeFn] = None,
) -> RunOutcome:
    """Run exactly one scheduler tick: recheck due providers, load real
    Trello state, revisit any blocked project that is due for an
    unattended recovery pass (see recovery.py - independent of the normal
    priority scheduler, which never picks a blocked project at all), and
    run at most one project. Never spends an AI token when there is
    nothing schedulable.

    A project sitting in Testování is never eligible for the normal
    implementation dispatch below (see scheduler.NOT_SCHEDULABLE_STATUSES).
    Each tick first resumes due ``Čeká na AI`` provider waits and gives a
    resumed implementation card the first dispatch opportunity. Only when
    no implementation wait was resumed does it drain one ``Testování``
    audit-only card; normal ``Pracuje se`` dispatch follows only when the
    audit gate is empty. Without ``audit_run_fn`` (e.g. an embedder that has
    not wired up the audit path yet) a Testování card simply stays put - never
    falling back into ordinary implementation dispatch either way.
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
            projects_root=projects_root,
            planner=inbox_planner,
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

        # Board maintenance writes migrations through its own strict
        # read/write pass. Refresh the scheduler snapshot afterward so a
        # stale in-memory ProjectRecord cannot immediately overwrite a newly
        # migrated dependency/order field during the same tick.
        prepared_this_tick_ids = {
            project.trello_card_id
            for project in projects
            if getattr(project, "_prepared_this_tick", False)
        }
        projects = fetch_all_projects(client, exclude_list_names=(inbox_list_name,))
        for project in projects:
            if project.trello_card_id in prepared_this_tick_ids:
                setattr(project, "_prepared_this_tick", True)
        # The pre-check above ran against the pre-refetch snapshot. This
        # fresh read is the one dispatch selection actually uses below, and
        # it can legitimately differ (board maintenance/migrations just ran,
        # or the first snapshot simply predates a card's own creation within
        # this same tick) - a project with no identity label must never slip
        # through on that gap. Incident: card "AI CAD - evidence Onshape
        # projektů a tisku na Bambu Lab A1" reached IN_PROGRESS with no
        # project_key label at all (2026-09-03), which only failed inside
        # the real dispatch attempt instead of being caught here first, and
        # then sat blocked-with-human-hold without ever consuming a token -
        # correct, but only by luck of where in run_fn the check happened to
        # live, not because this gate covered it.
        if project_paths is not None:
            _fail_closed_invalid_project_identities(client, projects, project_paths)

        if prepared_this_tick_ids:
            # Explicit tick boundary: a tick that just admitted new Inbox
            # work into Připraveno must end here, before audit selection,
            # scheduler selection, and dispatch even look at the board. This
            # keeps the freshly prepared card visible to a human for a full
            # tick and stops unrelated already-schedulable work (e.g. an
            # existing NEW/READY project, which is not gated by
            # ``intake_gate_statuses`` above) from being audited or
            # dispatched in the very same tick fresh intake happened. A tick
            # where intake prepared nothing new falls through unchanged.
            return RunOutcome(
                ran=False,
                reason=(
                    "Inbox intake připravil novou práci do Připraveno; tick končí "
                    "před audit/scheduler selection a dispatchem: "
                    + ", ".join(sorted(str(cid) for cid in prepared_this_tick_ids))
                ),
            )

        resumed_provider_waits = set(_resume_due_provider_waits(
            client,
            projects,
            provider_registry,
            providers_for_project=providers_for_project,
            default_providers=default_providers,
        ))

        audit_waits_resumed = {
            project.name for project in projects
            if project.name in resumed_provider_waits
            and project.extra_data.get("resume_status") is None
            and project.status == ProjectStatus.TESTING
        }
        if audit_waits_resumed:
            return RunOutcome(
                ran=False,
                reason=(
                    "auditní čekání znovu zařazeno do Testování bez nového AI volání: "
                    + ", ".join(sorted(audit_waits_resumed))
                ),
            )

        _run_recovery_pass(
            client,
            projects,
            provider_registry,
            max_attempts=recovery_max_attempts,
            backoff=recovery_backoff,
        )

        # Reflect the test/audit phase on Trello before the audit provider is
        # invoked. This local transition spends no AI tokens.
        _promote_completed_implementations_to_testing(client, projects, finalize_fn=finalize_fn)

        # New Inbox tasks were already admitted to Připraveno above. They are
        # intentionally not eligible for implementation dispatch until the
        # next tick; audits and recovery still see the complete board snapshot.
        dispatch_projects = [
            project for project in projects
            if not getattr(project, "_prepared_this_tick", False)
        ]

        outcome = None
        resumed_implementation_projects = [
            project
            for project in dispatch_projects
            if project.name in resumed_provider_waits
            and project.status in {ProjectStatus.READY, ProjectStatus.IN_PROGRESS}
        ]
        if resumed_implementation_projects:
            # A provider-limit wait is the first workflow phase. Once its
            # retry deadline/failover is satisfied, resume that checkpoint
            # before spending this tick on unrelated audit work.
            outcome = run_once(
                client,
                resumed_implementation_projects,
                provider_registry,
                run_fn,
                lock_manager=lock_manager,
                guard=guard,
                holder=holder,
                providers_for_project=providers_for_project,
                default_providers=default_providers,
                finalize_fn=finalize_fn,
            )

        if (outcome is None or not outcome.ran) and audit_run_fn is not None:
            # A provider wait is a state transition, not permission to spend
            # another AI call in the same tick. This guarantees the operator
            # can observe the requeued audit card before its next attempt.
            audit_projects = [
                project for project in projects
                if project.name not in resumed_provider_waits
            ]
            outcome = run_once_audit(
                client,
                audit_projects,
                provider_registry,
                audit_run_fn,
                lock_manager=lock_manager,
                guard=guard,
                holder=holder,
                providers_for_project=providers_for_project,
                default_providers=default_providers,
                finalize_fn=finalize_fn,
            )

        if outcome is None or not outcome.ran:
            outcome = run_once(
                client,
                dispatch_projects,
                provider_registry,
                run_fn,
                lock_manager=lock_manager,
                guard=guard,
                holder=holder,
                providers_for_project=providers_for_project,
                default_providers=default_providers,
                finalize_fn=finalize_fn,
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


def run_maintenance_only(
    client,
    *,
    project_paths: Optional[dict] = None,
    card_project_keys: Optional[dict] = None,
    projects_root: Optional[str] = None,
) -> list[str]:
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
    # Apply only explicit, stable identity migrations before the generic
    # contract pass. This repairs older Inbox children whose free-form label
    # pointed at an empty generated checkout, without dispatching any AI work
    # or changing priority/order after intake.
    projects = fetch_all_projects(
        client,
        exclude_list_names=("Inbox", "INBOX / Nápady"),
    )
    migrated = _bootstrap_project_keys(
        client,
        projects,
        project_paths=project_paths,
        card_project_keys=card_project_keys,
    )
    _register_generated_project_paths(projects, project_paths, projects_root)

    # Requeue only cards whose stale identity was repaired in this pass and
    # only when the replacement checkout exists. Provider-limit and ordinary
    # blocked cards are not in ``migrated`` and remain untouched.
    for project in migrated:
        configured_path = (project_paths or {}).get(project.project_key)
        if (
            project.status == ProjectStatus.BLOCKED
            and configured_path
            and Path(str(configured_path)).expanduser().is_dir()
        ):
            project.transition_to(ProjectStatus.NEW)
            project.blocked_by = None
            project.stop_reason = None
            project.retry_after = None
            project.human_notified_reason = None
            project.human_action_step = None
            sync_project_to_trello(client, project)
            notify(
                "[AI Project Manager] Opravena stale identita a znovu zařazena "
                f"karta do Připraveno: {project.name}{_card_url_suffix(project)}"
            )

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
    projects_root: Optional[str] = None,
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
    inbox_planner: Optional[InboxPlannerFn] = None,
    artifact_cleanup_root: Optional[str] = None,
    artifact_cleanup_retention_seconds: float = 86400.0,
    finalize_fn: Optional[FinalizeFn] = None,
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
                    projects_root=projects_root,
                    inbox_planner=inbox_planner,
                    recovery_max_attempts=recovery_max_attempts,
                    recovery_backoff=recovery_backoff,
                    audit_run_fn=audit_run_fn,
                    finalize_fn=finalize_fn,
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

        # run_tick is synchronous: reaching this point means neither an
        # implementation nor an audit subprocess is active. Cleanup is kept
        # outside that run boundary and is restricted again by the helper's
        # explicit active_run gate and filename allowlist.
        if artifact_cleanup_root:
            removed = cleanup_test_artifacts(
                artifact_cleanup_root,
                retention_seconds=artifact_cleanup_retention_seconds,
                active_run=False,
            )
            if removed:
                logger.info("removed %d expired test artifact(s)", len(removed))

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
