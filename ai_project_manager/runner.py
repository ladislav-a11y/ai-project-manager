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
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .guard import OrchestratorGuard
from .lock import ProjectLockError, ProjectLockManager
from .models import ProjectRecord, ProjectStatus
from .orchestrator_handoff import (
    AuditVerdictError,
    apply_audit_verdict,
    apply_dod_progress,
    dod_fully_verified,
    implementation_dod,
    materialize_project_dod,
)
from .providers import ProviderRegistry
from .scheduler import pick_next_audit_project, pick_next_project
from .trello_sync import sync_project_to_trello
from .slack_notify import notify, result_model, usage_suffix

logger = logging.getLogger("ai_project_manager")

# run_fn performs the actual provider/orchestrator call for one project
# and returns a result dict with any of: checkpoint, last_output,
# next_step, stop_reason, retry_after, status.
RunFn = Callable[[ProjectRecord, str], dict]

# audit_run_fn performs the audit-only ai-orchestrator call for one
# Testování project and returns a result dict with either an explicit
# "verdict" ("accepted"/"rejected", plus "reason"/"evidence"/
# "reject_target"/"checkpoint") or, when the provider hit a session
# limit, "retry_after"/"stop_reason" and no verdict at all - see
# orchestrator_runner.build_audit_run_fn.
AuditRunFn = Callable[[ProjectRecord, str], dict]

_REJECT_TARGET_MAP = {
    "in_progress": ProjectStatus.IN_PROGRESS,
    "ready": ProjectStatus.READY,
    "testing": ProjectStatus.TESTING,
}

DEFAULT_HOLDER = "project-manager"


def _capture_live_trello_readback(client, project: ProjectRecord) -> dict:
    """Capture compact, fresh Trello evidence immediately before an audit.

    The audit must be able to verify the card that is actually on the board,
    not only the ProjectRecord snapshot selected earlier in the tick. Keep
    the payload bounded and focused on lifecycle, identity, DoD, checkpoint,
    and Inbox/contract fields; the full card description is intentionally not
    copied into PM-DATA.
    """
    captured_at = datetime.now(timezone.utc).isoformat()
    if not project.trello_card_id:
        return {
            "status": "error",
            "captured_at": captured_at,
            "error": "project has no Trello card id",
        }

    try:
        card = client.get_card(project.trello_card_id)
        list_name = None
        list_lookup_error = None
        try:
            lists = client.list_lists()
            list_name = next(
                (
                    item.get("name")
                    for item in lists
                    if isinstance(item, dict) and item.get("id") == card.get("list_id")
                ),
                None,
            )
            if list_name is None:
                list_lookup_error = f"unknown list id {card.get('list_id')!r}"
        except Exception as exc:  # noqa: BLE001 - evidence records lookup failure
            list_lookup_error = str(exc)

        contract_keys = (
            "schema_version",
            "card_identity",
            "source_card_id",
            "source_card_url",
            "source_content_sha256",
            "source_priority",
            "task_priority",
            "scope",
            "inbox_receipts",
            "processed_inbox_card_ids",
            "dod_routing_policy",
            "governance",
        )
        contract_metadata = {
            key: project.extra_data[key]
            for key in contract_keys
            if key in project.extra_data
        }
        readback = {
            "status": "ok",
            "captured_at": captured_at,
            "card_id": card.get("id"),
            "card_name": card.get("name"),
            "card_url": card.get("url"),
            "list_id": card.get("list_id"),
            "list_name": list_name,
            "list_lookup_error": list_lookup_error,
            "labels": [
                label.get("name")
                for label in (card.get("labels") or [])
                if isinstance(label, dict) and label.get("name")
            ],
            "last_activity_at": card.get("last_activity_at"),
            "pm_data_present": "PM-DATA" in (card.get("desc") or ""),
            "lifecycle_status": project.status.value,
            "dod": [
                {
                    "index": index,
                    "text": item.text,
                    "checked": item.checked,
                    "phase": item.phase,
                }
                for index, item in enumerate(project.dod)
            ],
            "checkpoint": dict(project.checkpoint or {}),
            "contract_metadata": contract_metadata,
        }
        if list_lookup_error:
            readback["status"] = "partial"
        return readback
    except Exception as exc:  # noqa: BLE001 - never invent evidence
        return {
            "status": "error",
            "captured_at": captured_at,
            "card_id": project.trello_card_id,
            "error": str(exc),
        }


@dataclass
class RunOutcome:
    ran: bool
    project_name: Optional[str] = None
    provider: Optional[str] = None
    reason: Optional[str] = None
    halted: bool = False
    operational_error: bool = False
    # Set when a self-update was detected and safely prepared for restart
    # (see self_update.py / daemon.run_loop). The process should exit with
    # self_update.RESTART_REQUIRED_EXIT_CODE so a supervising watchdog -
    # never this process itself - performs the actual restart.
    restart_required: bool = False


def _apply_run_result(project: ProjectRecord, result: dict) -> None:
    reported_status = result.get("status")
    if reported_status in {"done", "testing"} and not project.dod:
        materialize_project_dod(project)
    if "checkpoint" in result:
        # JSON ``null`` is a legitimate way for an external orchestrator to
        # say that it has no resumable checkpoint.  Normalize it to the
        # ProjectRecord's canonical empty mapping instead of raising from
        # ``dict(None)`` after the subprocess already completed.
        project.checkpoint = dict(result["checkpoint"] or {})
    # Fold any newly-reported completed_dod_indices onto project.dod before
    # deciding whether "done" is actually allowed to stick below - the
    # checkpoint is the only place that progress is reported.
    apply_dod_progress(project, project.checkpoint)
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
        if status == "waiting_for_provider":
            new_status = ProjectStatus.PAUSED
        else:
            new_status = ProjectStatus(status) if isinstance(status, str) else status
        if new_status == ProjectStatus.DONE:
            if not dod_fully_verified(project):
                # Never close the card on an incomplete/unverified Definition
                # of Done - keep it schedulable so a later run can finish and
                # verify the remaining item(s) instead of silently losing them.
                new_status = ProjectStatus.IN_PROGRESS
                implementation_items = implementation_dod(project)
                remaining = sum(1 for item in implementation_items if not item.checked)
                project.stop_reason = (
                    f"orchestrator reported done but {remaining} of {len(implementation_items)} "
                    "Definition of Done item(s) are not yet verified as completed - "
                    "refusing to close the card"
                )
                if project.returned_from_testing:
                    project.extra_data["resume_status"] = ProjectStatus.IN_PROGRESS.value
            else:
                # An implementation agent cannot close a card. Even a fully
                # checked implementation DoD must be independently audited by
                # ai-orchestrator before apply_audit_verdict may move it to
                # Hotovo.
                new_status = ProjectStatus.TESTING
                project.stop_reason = (
                    "implementation reported done; awaiting ai-orchestrator audit"
                )
        elif new_status == ProjectStatus.BLOCKED and (
            not project.stop_reason or project.stop_reason.strip().lower() == "blocked"
        ):
            remaining = [item.text for item in implementation_dod(project) if not item.checked]
            concrete_next = (project.next_step or "").strip()
            if not concrete_next and remaining:
                concrete_next = remaining[0]
            detail = concrete_next or "orchestrator must provide a concrete blocking reason"
            project.stop_reason = (
                "orchestrator returned generic blocked without an actionable cause; "
                f"next unresolved step: {detail}"
            )
        project.transition_to(new_status)


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
    lock_manager = lock_manager or ProjectLockManager()
    guard = guard or OrchestratorGuard()

    # Do not let a busy high-priority project monopolize every scheduler
    # tick.  This is only an optimistic filter: ``hold`` below remains the
    # authoritative, race-safe lock acquisition.
    unlocked_projects = [
        project
        for project in projects
        if not lock_manager.is_locked_by_other(project.name, holder)
    ]
    decision = pick_next_project(
        unlocked_projects,
        provider_registry,
        providers_for_project=providers_for_project,
        default_providers=default_providers,
    )
    if decision is None:
        logger.info("no schedulable project with an available provider")
        return RunOutcome(ran=False, reason="no schedulable project with an available provider")

    project = decision.project
    provider = decision.provider
    selected_model = provider_registry.selected_model(provider)
    provider_detail = f"{provider} | model: {selected_model or 'provider default (nezjištěn)'}"
    logger.info("selected project=%r provider=%s", project.name, provider)

    try:
        with lock_manager.hold(project.name, holder):
            # Announce only after the lock is actually held.  Another worker
            # may win the race after the optimistic filter above.
            notify(f"[AI Project Manager] Zahajuji: {project.name} | provider: {provider_detail}")
            project.provider = provider
            project.extra_data["provider_selection"] = {
                "provider": provider,
                "model": selected_model,
                "source": "AI_PM_PROVIDER_MODELS" if selected_model else "provider_default",
            }
            # The Trello board must show the real flow while the provider is
            # working, not leave an active card looking idle in Připraveno.
            project.transition_to(ProjectStatus.IN_PROGRESS)
            sync_project_to_trello(client, project)
            logger.info(
                "dispatching project=%r to provider=%s in autonomous mode (checkpoint=%s)",
                project.name, provider, project.checkpoint,
            )
            try:
                result = run_fn(project, provider)
                if not isinstance(result, dict):
                    raise TypeError(
                        "orchestrator result must be a mapping, got "
                        f"{type(result).__name__}"
                    )
                _apply_run_result(project, result)
            except Exception as exc:  # noqa: BLE001 - run failures are reported on the card, not raised
                signature = str(exc)
                halted = guard.record_denial(project.name, signature)
                project.stop_reason = signature
                if halted:
                    project.transition_to(ProjectStatus.BLOCKED)
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
            # Only reset the unattended-recovery attempt counter (see
            # recovery.py) once the project is actually no longer blocked.
            # A run that immediately reports "blocked" again for the same
            # underlying reason must NOT wipe the counter - that would
            # silently defeat recovery's max-attempts loop guard by
            # letting an unfixable block requeue forever, once per tick.
            if not project.is_blocked:
                project.recovery_attempts = 0
            logger.info(
                "run result project=%r provider=%s status=%s stop_reason=%s retry_after=%s",
                project.name, provider, project.status.value, project.stop_reason, project.retry_after,
            )
            actual_provider = result.get("active_provider") or provider
            confirmed_model = result_model(result)
            if confirmed_model:
                # Learn the model actually used from ai-orchestrator's receipt;
                # this becomes the preferred model shown on the next dispatch.
                provider_registry.configure_models(provider, [confirmed_model])
            actual_model = confirmed_model or selected_model or "provider default (nezjištěn)"
            project.extra_data["provider_selection"]["actual_provider"] = actual_provider
            project.extra_data["provider_selection"]["actual_model"] = actual_model
            sync_project_to_trello(client, project)
            logger.info("synced project=%r state to trello (card=%s)", project.name, project.trello_card_id)
            if project.status == ProjectStatus.DONE:
                notify(
                    f"[AI Project Manager] Dokončeno: {project.name} | provider: {actual_provider} | model: {actual_model} | výsledek zapsán do Trella"
                    + usage_suffix(result)
                )
            elif project.retry_after:
                notify(
                    f"[AI Project Manager] Čeká na obnovení limitu: {project.name} | další pokus: {project.retry_after}"
                )
            else:
                next_step = project.next_step or project.stop_reason or "pokračování v dalším běhu"
                notify(
                    f"[AI Project Manager] Průběžný stav: {project.name} | provider: {actual_provider} | model: {actual_model} | další krok: {next_step}"
                    + usage_suffix(result)
                )
            return RunOutcome(
                ran=True,
                project_name=project.name,
                provider=provider,
                reason=project.stop_reason,
            )
    except ProjectLockError as exc:
        logger.info("project=%r locked by another worker, skipping: %s", project.name, exc)
        return RunOutcome(ran=False, project_name=project.name, provider=provider, reason=str(exc))


def run_once_audit(
    client,
    projects: list[ProjectRecord],
    provider_registry: ProviderRegistry,
    audit_run_fn: AuditRunFn,
    lock_manager: Optional[ProjectLockManager] = None,
    guard: Optional[OrchestratorGuard] = None,
    holder: str = DEFAULT_HOLDER,
    providers_for_project: Optional[dict] = None,
    default_providers: Optional[list] = None,
) -> RunOutcome:
    """Run the audit-only path for exactly one Testování project, if any
    is currently awaiting an ai-orchestrator verdict.

    This is a separate entry point from ``run_once`` on purpose: a
    Testování card is never eligible for ``pick_next_project`` (see
    scheduler.NOT_SCHEDULABLE_STATUSES), so it can only ever reach
    ai-orchestrator through here, and the only thing this ever applies to
    the card's lifecycle is the verdict ai-orchestrator itself reported
    (via ``orchestrator_handoff.apply_audit_verdict``) - never a status
    this process derives on its own.
    """
    lock_manager = lock_manager or ProjectLockManager()
    guard = guard or OrchestratorGuard()

    unlocked_projects = [
        project
        for project in projects
        if not lock_manager.is_locked_by_other(project.name, holder)
    ]
    decision = pick_next_audit_project(
        unlocked_projects,
        provider_registry,
        providers_for_project=providers_for_project,
        default_providers=default_providers,
    )
    if decision is None:
        logger.info("no project awaiting audit with an available provider")
        return RunOutcome(ran=False, reason="no project awaiting audit with an available provider")

    project = decision.project
    provider = decision.provider
    selected_model = provider_registry.selected_model(provider)
    provider_detail = f"{provider} | model: {selected_model or 'provider default (nezjištěn)'}"
    logger.info("selected project=%r provider=%s for audit", project.name, provider)

    try:
        with lock_manager.hold(project.name, holder):
            if not dod_fully_verified(project):
                # Testování is only an audit gate after implementation DoD is
                # complete. Returning an incomplete card to Pracuje se avoids
                # spending audit-provider tokens on work that is not ready
                # for an independent verdict.
                implementation_items = implementation_dod(project)
                remaining = sum(1 for item in implementation_items if not item.checked)
                project.stop_reason = (
                    f"audit odložen: {remaining} z {len(implementation_items)} "
                    "DoD bodů ještě není ověřeno; pokračovat v implementaci"
                )
                project.mark_returned_from_testing("incomplete_dod")
                project.transition_to(ProjectStatus.IN_PROGRESS)
                sync_project_to_trello(client, project)
                notify(
                    f"[AI Project Manager] Audit odložen: {project.name} | "
                    "neúplné DoD vráceno do Pracuje se bez volání AI"
                )
                return RunOutcome(
                    ran=True,
                    project_name=project.name,
                    provider=provider,
                    reason=project.stop_reason,
                )
            notify(f"[AI Project Manager] Zahajuji audit: {project.name} | provider: {provider_detail}")
            project.provider = provider
            project.extra_data["provider_selection"] = {
                "provider": provider,
                "model": selected_model,
                "source": "AI_PM_PROVIDER_MODELS" if selected_model else "provider_default",
            }
            sync_project_to_trello(client, project)
            logger.info(
                "dispatching project=%r to provider=%s in audit-only mode (checkpoint=%s)",
                project.name, provider, project.checkpoint,
            )
            try:
                # Refresh the durable source of truth immediately before
                # building the audit handoff. This evidence is persisted by
                # the unconditional sync below, so a failed/rejected audit
                # leaves the exact board readback available for the next
                # attempt instead of forcing the auditor to speculate.
                project.extra_data["live_trello_readback"] = _capture_live_trello_readback(
                    client, project
                )
                result = audit_run_fn(project, provider)
                if not isinstance(result, dict):
                    raise TypeError(
                        "audit orchestrator result must be a mapping, got "
                        f"{type(result).__name__}"
                    )
                verdict = result.get("verdict")
                if verdict is not None:
                    reject_target = None
                    raw_reject_target = result.get("reject_target")
                    if raw_reject_target is not None:
                        if raw_reject_target not in _REJECT_TARGET_MAP:
                            raise AuditVerdictError(
                                f"invalid audit reject_target {raw_reject_target!r}; expected one of "
                                f"{sorted(_REJECT_TARGET_MAP)}"
                            )
                        reject_target = _REJECT_TARGET_MAP[raw_reject_target]
                    if "checkpoint" in result:
                        project.checkpoint = dict(result["checkpoint"] or {})
                    apply_audit_verdict(
                        project,
                        verdict,
                        reason=result.get("reason"),
                        evidence=result.get("evidence"),
                        reject_target=reject_target,
                        rejected_indices=result.get("rejected_indices"),
                    )
                else:
                    # A provider/session limit is a workflow wait, not an
                    # audit result. Keep the return phase so the next tick
                    # resumes with the audit path (never ordinary work).
                    if "checkpoint" in result:
                        project.checkpoint = dict(result["checkpoint"] or {})
                    if "stop_reason" in result:
                        project.stop_reason = result["stop_reason"]
                    if "retry_after" in result:
                        project.retry_after = result["retry_after"]
                    if result.get("status") == "paused" and project.retry_after:
                        project.extra_data["resume_status"] = ProjectStatus.TESTING.value
                        project.transition_to(ProjectStatus.PAUSED)
            except Exception as exc:  # noqa: BLE001 - audit failures are reported on the card, not raised
                signature = str(exc)
                # An audit provider that cannot return a verdict must not be
                # selected again on the very next tick. Keep the card in the
                # audit phase, preserve its checkpoint, and let the normal
                # provider selector fail over after a short local backoff.
                provider_registry.mark_error(
                    provider,
                    signature,
                    retry_after=timedelta(minutes=5),
                    checkpoint=project.checkpoint,
                )
                halted = guard.record_denial(project.name, signature)
                project.stop_reason = signature
                if halted:
                    project.transition_to(ProjectStatus.BLOCKED)
                    project.blocked_by = f"repeated audit failure: {signature}"
                logger.warning(
                    "audit failed project=%r provider=%s error=%s halted=%s",
                    project.name, provider, signature, halted,
                )
                sync_project_to_trello(client, project)
                notify("Audit error: " + project.name + " | " + signature)
                return RunOutcome(
                    ran=True,
                    project_name=project.name,
                    provider=provider,
                    reason=signature,
                    halted=halted,
                )
            guard.reset(project.name)
            actual_provider = result.get("active_provider") or provider
            confirmed_model = result_model(result)
            if confirmed_model:
                provider_registry.configure_models(provider, [confirmed_model])
            actual_model = confirmed_model or selected_model or "provider default (nezjištěn)"
            project.extra_data["provider_selection"]["actual_provider"] = actual_provider
            project.extra_data["provider_selection"]["actual_model"] = actual_model
            sync_project_to_trello(client, project)
            logger.info(
                "audit result project=%r provider=%s status=%s stop_reason=%s",
                project.name, provider, project.status.value, project.stop_reason,
            )
            if project.status == ProjectStatus.DONE:
                notify(
                    f"[AI Project Manager] Audit přijat: {project.name} | provider: {actual_provider} | model: {actual_model} | přesunuto do Hotovo"
                    + usage_suffix(result)
                )
            elif project.retry_after:
                notify(
                    f"[AI Project Manager] Audit čeká na obnovení limitu: {project.name} | další pokus: {project.retry_after}"
                )
            else:
                notify(
                    f"[AI Project Manager] Audit odmítnut: {project.name} | provider: {actual_provider} | model: {actual_model} | "
                    f"vráceno do {project.status.value} | důvod: {project.stop_reason}"
                    + usage_suffix(result)
                )
            return RunOutcome(
                ran=True,
                project_name=project.name,
                provider=provider,
                reason=project.stop_reason,
            )
    except ProjectLockError as exc:
        logger.info("project=%r locked by another worker, skipping audit: %s", project.name, exc)
        return RunOutcome(ran=False, project_name=project.name, provider=provider, reason=str(exc))
