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
from .providers import ProviderRegistry, TASK_AUDIT, TASK_IMPLEMENTATION, supports_model_selection
from .scheduler import (
    audit_capability_key,
    expand_provider_aliases,
    pick_next_audit_project,
    pick_next_project,
)
from .task_classification import classify_task, infer_complexity
from .trello_sync import project_from_card, sync_project_to_trello
from .slack_notify import (
    notify,
    provider_blocked_message,
    provider_selection_comparison,
    result_model,
    provider_route_detail,
    status_message,
    usage_suffix,
)

logger = logging.getLogger("ai_project_manager")

_AUDIT_CAPABILITY_LIMIT_MARKERS = (
    "needs verification",
    "pending audit verdict",
    "audit nebyl proveden",
    "nelze samostatně potvrdit",
    "nelze samostatne potvrdit",
    "agent nemá přístup",
    "agent nema pristup",
    "no live verification",
)


def _audit_capability_failure(result: dict) -> bool:
    """Recognize a plan-without-evidence rejection, not an ordinary bug finding."""
    if result.get("verdict") != "rejected":
        return False
    text = " ".join(str(result.get(key) or "") for key in ("reason", "evidence", "stop_reason")).casefold()
    return sum(marker in text for marker in _AUDIT_CAPABILITY_LIMIT_MARKERS) >= 2

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

# finalize_fn commits/pushes a card whose implementation DoD is fully
# checked, before it may be promoted to Testování - the controller-owned
# step from AGENTS.md rule 4/11 and AI_PROJECT_RUNTIME.md's controller
# finalization contract. Returns a result dict with "status" ("done" or
# "blocked"), and on success "checkpoint" (carrying the verified
# "finalization" proof) or "already_verified" when nothing was dirty; on
# failure "stop_reason" explains why promotion must not happen yet. See
# orchestrator_runner.build_finalize_fn.
FinalizeFn = Callable[[ProjectRecord], dict]

_REJECT_TARGET_MAP = {
    "in_progress": ProjectStatus.IN_PROGRESS,
    "ready": ProjectStatus.READY,
    "testing": ProjectStatus.TESTING,
}

DEFAULT_HOLDER = "project-manager"


def _display_model(provider: str, model: Optional[str]) -> str:
    """Render the model confirmed by the provider, or its unknown default."""
    return model or "provider default (nezjištěn)"


def _task_type_label(task_type: str) -> str:
    """Return the human-facing task family used in Slack explanations."""
    return {
        TASK_IMPLEMENTATION: "implementaci",
        TASK_AUDIT: "audit",
    }[task_type]


def _model_selection_reason(
    provider: str,
    model: Optional[str],
    provider_registry: ProviderRegistry,
    task_type: str,
    *,
    complexity: Optional[str] = None,
    confirmed: bool = False,
) -> str:
    """Explain the LLM choice independently from the provider choice.

    ``complexity`` (see ``task_classification.infer_complexity``) resolves
    the target model quality tier through ``task_classification.
    classify_task`` and names it in the explanation. It deliberately never
    names a provider's configured catalog entry for that tier here - unlike
    Inbox planning's ``_inbox_model_hint``, an unconfirmed catalog value must
    not leak into this reason (see
    test_run_once_does_not_report_unconfirmed_configured_model) precisely
    because PM never forwards ``--model`` for implementation/audit, so that
    value is not even a real hint of what will run. Once ai-orchestrator
    confirms an actual model, the ``confirmed`` branch above reports it.
    """
    task_label = _task_type_label(task_type)
    if confirmed and model:
        return (
            f"model {model} je pro {task_label} skutečně použitý model providera "
            "potvrzený ai-orchestrátorem"
        )
    tier_detail = ""
    if complexity is not None:
        model_tier = classify_task(task_type, complexity).model_tier
        tier_detail = f"; cílová úroveň {model_tier} pro náročnost {complexity}"
    if model:
        return f"PM požaduje model {model} pro {task_label}{tier_detail}"
    return f"provider použije podporované výchozí chování pro {task_label}{tier_detail}"


def _provider_selection_reason(
    project: ProjectRecord,
    provider: str,
    providers_for_project: Optional[dict],
    default_providers: Optional[list],
    provider_registry: ProviderRegistry,
    task_type: str,
    selected_model: Optional[str] = None,
) -> str:
    ordered = (providers_for_project or {}).get(
        project.name,
        default_providers or provider_registry.registered_names(),
    )
    resolved_order = expand_provider_aliases(list(ordered), provider_registry)
    provider_reason = f"provider je první dostupný v pořadí {', '.join(resolved_order or ordered)}"
    selection = (project.extra_data or {}).get("provider_selection") or {}
    model_reason = _model_selection_reason(
        provider, selected_model or selection.get("selected_model") or selection.get("model"),
        provider_registry, task_type, complexity=infer_complexity(project)
    )
    return f"{provider_reason}; {model_reason}"


def _actual_provider_selection_reason(
    project: ProjectRecord,
    selected_provider: str,
    actual_provider: str,
    actual_model: Optional[str],
    confirmed_model: Optional[str],
    providers_for_project: Optional[dict],
    default_providers: Optional[list],
    provider_registry: ProviderRegistry,
    task_type: str,
) -> str:
    """Explain the provider and model that the orchestrator actually used."""
    if actual_provider == selected_provider:
        model_reason = _model_selection_reason(
            actual_provider,
            actual_model,
            provider_registry,
            task_type,
            complexity=infer_complexity(project),
            confirmed=bool(confirmed_model),
        )
        return (
            f"receipt ai-orchestrátoru potvrdil požadovaný provider {actual_provider}; "
            f"{model_reason}"
        )

    provider_reason = (
        f"provider {actual_provider} byl použit po failoveru z {selected_provider}"
    )
    model_reason = _model_selection_reason(
        actual_provider,
        actual_model,
        provider_registry,
        task_type,
        complexity=infer_complexity(project),
        confirmed=bool(confirmed_model),
    )
    return f"{provider_reason}; {model_reason}"


def _capture_live_trello_readback(client, project: ProjectRecord) -> dict:
    """Capture compact, fresh Trello evidence immediately before an audit.

    The audit must be able to verify the card that is actually on the board,
    not only the ProjectRecord snapshot selected earlier in the tick. Every
    fact below (priority, lifecycle status, DoD, checkpoint, contract
    metadata) is re-derived by fetching the live card and re-parsing it with
    ``project_from_card`` - the same read path a fresh scheduler tick would
    use - so the evidence proves the round trip through Trello, never an
    unverified echo of the agent's in-memory claim. Keep the payload bounded
    and focused on lifecycle, identity, DoD, checkpoint, and Inbox/contract
    fields; the full card description is intentionally not copied into
    PM-DATA.
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
        id_to_name: dict = {}
        try:
            lists = client.list_lists()
            id_to_name = {
                item.get("id"): item.get("name")
                for item in lists
                if isinstance(item, dict) and item.get("id")
            }
            list_name = id_to_name.get(card.get("list_id"))
            if list_name is None:
                list_lookup_error = f"unknown list id {card.get('list_id')!r}"
        except Exception as exc:  # noqa: BLE001 - evidence records lookup failure
            list_lookup_error = str(exc)

        # Re-derive every DoD/checkpoint/contract fact from the card that was
        # just fetched, instead of echoing ``project`` (the pre-sync in-memory
        # claim). This is the only way the evidence proves the write -> Trello
        # -> project_from_card round trip and Card Contract migration actually
        # preserved priority, identity, dependency and workflow-state data,
        # rather than merely repeating what the agent asserted before sync.
        live_project = project_from_card(card, id_to_name)

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
            # Carries the Inbox split's priority, identity (source card id/
            # url/content hash, subtask index), dependency
            # (depends_on_subtask_indices) and workflow-state (execution_order,
            # project_path) metadata, so the pre-audit evidence can prove that
            # migration/readback never dropped or reordered it.
            "inbox_preparation",
            "dod_routing_policy",
            "governance",
            "provider_selection",
            "provider_selection_history",
            "provider_statuses",
        )
        contract_metadata = {
            key: live_project.extra_data[key]
            for key in contract_keys
            if key in live_project.extra_data
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
            "priority": live_project.priority,
            "lifecycle_status": live_project.status.value,
            "dod": [
                {
                    "index": index,
                    "text": item.text,
                    "checked": item.checked,
                    "phase": item.phase,
                }
                for index, item in enumerate(live_project.dod)
            ],
            "checkpoint": dict(live_project.checkpoint or {}),
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


def _remember_provider_selection(project: ProjectRecord) -> None:
    """Keep bounded, secret-free implementation handoff receipts for audit.

    The audit phase temporarily selects its own provider and must not erase
    the implementation provider/model evidence that the preceding PM run
    produced. Receipts contain only routing metadata and are persisted in
    Trello's PM-DATA block; prompts, credentials, and raw provider output are
    never copied.
    """
    selection = project.extra_data.get("provider_selection")
    if not isinstance(selection, dict) or selection.get("stage") == "audit":
        return
    history = project.extra_data.get("provider_selection_history")
    if not isinstance(history, list):
        history = []
    run_id = selection.get("run_id")
    if run_id and any(
        isinstance(item, dict) and item.get("run_id") == run_id for item in history
    ):
        return
    history.append(dict(selection))
    project.extra_data["provider_selection_history"] = history[-8:]


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


def _apply_run_result(
    project: ProjectRecord, result: dict, finalize_fn: Optional[FinalizeFn] = None
) -> None:
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
    # Keep the complete AO per-provider receipt in PM-DATA/Trello. This is
    # not a replacement for the persistent ProviderRegistry; it is the
    # human-auditable copy showing every provider and its retry deadline.
    provider_statuses = result.get("provider_statuses")
    if isinstance(provider_statuses, dict):
        project.extra_data["provider_statuses"] = provider_statuses
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
                # Hotovo. A plain implementation DoD item never asks the
                # controller finalizer to run by name (see
                # orchestrator_runner._finalization_indices), so this is the
                # only place - besides daemon._promote_completed_
                # implementations_to_testing's own catch-up check for a card
                # that reaches "all implementation items checked" without a
                # fresh run_fn call in the same tick - that actually commits
                # the verified work before Testování. Without it the
                # independent audit always finds an unchanged checkout (see
                # incident: card P5.20, Station Agent - oprava P5).
                if finalize_fn is not None:
                    finalize_result = finalize_fn(project) or {}
                    if finalize_result.get("status") != "done":
                        reason = finalize_result.get("stop_reason") or "controller finalization failed"
                        new_status = ProjectStatus.IN_PROGRESS
                        project.stop_reason = (
                            "implementation DoD complete but controller finalization failed: "
                            f"{reason}"
                        )
                    else:
                        if not finalize_result.get("already_verified"):
                            project.checkpoint = finalize_result.get("checkpoint", project.checkpoint)
                        new_status = ProjectStatus.TESTING
                        project.stop_reason = (
                            "implementation reported done; awaiting ai-orchestrator audit"
                        )
                else:
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
    finalize_fn: Optional[FinalizeFn] = None,
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
    classification = classify_task(TASK_IMPLEMENTATION, infer_complexity(project))
    selector = getattr(run_fn, "select_model", None)
    selected_model = (
        selector(project, provider) if callable(selector)
        else (
            provider_registry.model_for_tier(provider, classification.model_tier)
            if supports_model_selection(provider) else None
        )
    )
    provider_detail = f"{provider} | model: {_display_model(provider, selected_model)}"
    provider_reason = _provider_selection_reason(
        project, provider, providers_for_project, default_providers, provider_registry,
        TASK_IMPLEMENTATION, selected_model
    )
    logger.info("selected project=%r provider=%s", project.name, provider)

    try:
        with lock_manager.hold(project.name, holder):
            # Announce only after the lock is actually held.  Another worker
            # may win the race after the optimistic filter above.
            notify(status_message(
                "PM zahajuje práci (Zahajuji)",
                project=project.name,
                provider=provider_detail,
                provider_reason=provider_reason,
            ))
            project.provider = provider
            project.extra_data["provider_selection"] = {
                "provider": provider,
                # ``model`` is receipt-owned. Keep the requested value
                # separately so an unconfirmed request is never presented as
                # the model the provider actually used.
                "model": None,
                "selected_model": selected_model,
                "selected_provider": provider,
                "requested_reason": provider_reason,
                "stage": "implementation",
                "source": "AI_PM_PROVIDER_MODELS" if selected_model else "provider_default",
                "classification": classification.as_dict(),
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
                _apply_run_result(project, result, finalize_fn=finalize_fn)
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
                notify(status_message(
                    "PM skončil chybou providera",
                    project=project.name,
                    provider=provider,
                    detail=f"důvod: {signature}",
                ))
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
            if confirmed_model and not provider_registry.get_status(actual_provider).models:
                # Learn the model actually used from ai-orchestrator's receipt;
                # this becomes the preferred model shown on the next dispatch
                # only when no operator-configured catalog exists. Replacing an
                # existing catalog here would discard its audit-quality model
                # before the subsequent implementation -> audit dispatch.
                provider_registry.configure_models(actual_provider, [confirmed_model])
            actual_model = confirmed_model
            actual_model_detail = _display_model(actual_provider, actual_model)
            actual_provider_reason = _actual_provider_selection_reason(
                project,
                provider,
                actual_provider,
                actual_model,
                confirmed_model,
                providers_for_project,
                default_providers,
                provider_registry,
                TASK_IMPLEMENTATION,
            )
            project.provider = actual_provider
            project.extra_data["provider_selection"]["selected_provider"] = provider
            project.extra_data["provider_selection"]["selected_model"] = selected_model
            project.extra_data["provider_selection"]["provider"] = actual_provider
            project.extra_data["provider_selection"]["model"] = actual_model
            project.extra_data["provider_selection"]["actual_provider"] = actual_provider
            project.extra_data["provider_selection"]["actual_model"] = actual_model
            project.extra_data["provider_selection"]["provider_sequence"] = [
                item for item in (result.get("provider_sequence") or [])
                if isinstance(item, str) and item.strip()
            ]
            project.extra_data["provider_selection"]["run_id"] = result.get("run_id")
            project.extra_data["provider_selection"]["recorded_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            project.extra_data["provider_selection"]["provider_reason"] = actual_provider_reason
            project.extra_data["provider_selection"]["actual_reason"] = actual_provider_reason
            route_detail = provider_route_detail(result, selected_provider=provider)
            project.extra_data["provider_selection"]["route_detail"] = route_detail
            comparison_detail = provider_selection_comparison(project.extra_data["provider_selection"])
            project.extra_data["provider_selection"]["live_evidence"] = {
                "source": "ai-orchestrator outbox",
                "active_provider": actual_provider,
                "active_model": actual_model,
                "provider_sequence": project.extra_data["provider_selection"]["provider_sequence"],
                "run_id": result.get("run_id"),
            }
            status_messages: list[str] = []
            if project.status == ProjectStatus.DONE:
                status_messages.append(
                    status_message(
                        "PM dokončil práci",
                        project=project.name,
                        provider=f"{actual_provider} | model: {actual_model_detail}",
                        provider_reason=actual_provider_reason,
                        detail=(
                            "výsledek zapsán do Trella | "
                            + comparison_detail + " | " + route_detail
                        ),
                    )
                    + usage_suffix(result)
                )
            elif project.retry_after:
                status_messages.extend([
                    provider_blocked_message(
                        actual_provider,
                        project.retry_after,
                        reason=project.stop_reason,
                    ),
                    status_message(
                        "PM ukončil tick a čeká",
                        project=project.name,
                        provider=f"{actual_provider} | model: {actual_model_detail}",
                        provider_reason=actual_provider_reason,
                        detail=(
                            f"další pokus: {project.retry_after} | "
                            + comparison_detail + " | " + route_detail
                        ),
                    ) + usage_suffix(result),
                ])
            else:
                next_step = project.next_step or project.stop_reason or "pokračování v dalším běhu"
                status_messages.append(
                    status_message(
                        "Průběžný stav: PM ukončil tick",
                        project=project.name,
                        provider=f"{actual_provider} | model: {actual_model_detail}",
                        provider_reason=actual_provider_reason,
                        detail=(
                            f"další krok: {next_step} | "
                            f"{comparison_detail} | {route_detail}"
                        ),
                    )
                    + usage_suffix(result)
                )
            deliveries = []
            for message in status_messages:
                deliveries.append({"message": message, "delivered": notify(message)})
            project.extra_data["provider_selection"]["slack_notifications"] = deliveries
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
    finalize_fn: Optional[FinalizeFn] = None,
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
    classification = classify_task(TASK_AUDIT, infer_complexity(project))
    selector = getattr(audit_run_fn, "select_model", None)
    selected_model = (
        selector(project, provider) if callable(selector)
        else (
            provider_registry.model_for_tier(provider, classification.model_tier)
            if supports_model_selection(provider) else None
        )
    )
    provider_detail = f"{provider} | model: {_display_model(provider, selected_model)}"
    provider_reason = _provider_selection_reason(
        project, provider, providers_for_project, default_providers, provider_registry,
        TASK_AUDIT, selected_model
    )
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
                notify(status_message(
                    "PM odložil audit",
                    project=project.name,
                    detail="neúplné DoD vráceno do Pracuje se bez volání AI",
                ))
                return RunOutcome(
                    ran=True,
                    project_name=project.name,
                    provider=provider,
                    reason=project.stop_reason,
                )
            if finalize_fn is not None:
                # A card can reach Testování with its implementation DoD
                # complete but not yet committed - either because it was
                # promoted before this defense-in-depth check existed
                # (already the case for older, stuck cards - see incident:
                # P5.20, Station Agent - oprava P5, 2026-09-03), or because
                # a future code path adds a new way to reach Testování. Fail
                # closed here too, one more time, before spending a real
                # audit-provider call on a checkout the independent audit
                # would just find unchanged.
                finalize_result = finalize_fn(project) or {}
                if finalize_result.get("status") != "done":
                    reason = finalize_result.get("stop_reason") or "controller finalization failed"
                    project.stop_reason = (
                        "implementation DoD complete but controller finalization failed: "
                        f"{reason}"
                    )
                    project.mark_returned_from_testing("finalization_failed")
                    project.transition_to(ProjectStatus.IN_PROGRESS)
                    sync_project_to_trello(client, project)
                    notify(status_message(
                        "PM odložil audit",
                        project=project.name,
                        detail=f"controller finalizace selhala: {reason}",
                    ))
                    return RunOutcome(
                        ran=True,
                        project_name=project.name,
                        provider=provider,
                        reason=project.stop_reason,
                    )
                if not finalize_result.get("already_verified"):
                    project.checkpoint = finalize_result.get("checkpoint", project.checkpoint)
            notify(status_message(
                "PM zahajuje audit",
                project=project.name,
                provider=provider_detail,
                provider_reason=provider_reason,
            ))
            project.provider = provider
            _remember_provider_selection(project)
            project.extra_data["provider_selection"] = {
                "provider": provider,
                # ``model`` is receipt-owned; ``selected_model`` is only the
                # concrete request sent to ai-orchestrator.
                "model": None,
                "selected_model": selected_model,
                "selected_provider": provider,
                "requested_reason": provider_reason,
                "stage": "audit",
                "source": "AI_PM_PROVIDER_MODELS" if selected_model else "provider_default",
                "classification": classification.as_dict(),
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
                if isinstance(result.get("provider_statuses"), dict):
                    project.extra_data["provider_statuses"] = result["provider_statuses"]
                if _audit_capability_failure(result):
                    capability_key = audit_capability_key(project)
                    actual_provider = result.get("active_provider") or provider
                    if capability_key:
                        provider_registry.mark_capability_limited(
                            actual_provider,
                            capability_key,
                            "audit returned a review plan without concrete evidence or an independent verdict",
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
                notify(status_message(
                    "PM ukončil audit chybou providera",
                    project=project.name,
                    provider=provider,
                    detail=f"důvod: {signature}",
                ))
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
            if confirmed_model and not provider_registry.get_status(actual_provider).models:
                provider_registry.configure_models(actual_provider, [confirmed_model])
            actual_model = confirmed_model
            actual_model_detail = _display_model(actual_provider, actual_model)
            actual_provider_reason = _actual_provider_selection_reason(
                project,
                provider,
                actual_provider,
                actual_model,
                confirmed_model,
                providers_for_project,
                default_providers,
                provider_registry,
                TASK_AUDIT,
            )
            project.provider = actual_provider
            project.extra_data["provider_selection"]["selected_provider"] = provider
            project.extra_data["provider_selection"]["selected_model"] = selected_model
            project.extra_data["provider_selection"]["provider"] = actual_provider
            project.extra_data["provider_selection"]["model"] = actual_model
            project.extra_data["provider_selection"]["actual_provider"] = actual_provider
            project.extra_data["provider_selection"]["actual_model"] = actual_model
            project.extra_data["provider_selection"]["provider_reason"] = actual_provider_reason
            project.extra_data["provider_selection"]["actual_reason"] = actual_provider_reason
            route_detail = provider_route_detail(result, selected_provider=provider)
            project.extra_data["provider_selection"]["route_detail"] = route_detail
            comparison_detail = provider_selection_comparison(project.extra_data["provider_selection"])
            if isinstance(result.get("provider_statuses"), dict):
                project.extra_data["provider_selection"]["provider_statuses"] = result["provider_statuses"]
            sync_project_to_trello(client, project)
            logger.info(
                "audit result project=%r provider=%s status=%s stop_reason=%s",
                project.name, provider, project.status.value, project.stop_reason,
            )
            if project.status == ProjectStatus.DONE:
                notify(
                    status_message(
                        "PM dokončil audit: přijato",
                        project=project.name,
                        provider=f"{actual_provider} | model: {actual_model_detail}",
                        provider_reason=actual_provider_reason,
                        detail=(
                            "přesunuto do Hotovo | "
                            + comparison_detail + " | " + route_detail
                        ),
                    )
                    + usage_suffix(result)
                )
            elif project.retry_after:
                notify(provider_blocked_message(
                    actual_provider,
                    project.retry_after,
                    reason=project.stop_reason,
                ))
                notify(status_message(
                    "PM ukončil auditní tick a čeká",
                    project=project.name,
                    provider=f"{actual_provider} | model: {actual_model_detail}",
                    provider_reason=actual_provider_reason,
                    detail=(
                        f"další pokus: {project.retry_after} | "
                        + comparison_detail + " | " + route_detail
                    ),
                ) + usage_suffix(result))
            else:
                notify(
                    status_message(
                        "PM dokončil audit: odmítnuto",
                        project=project.name,
                        provider=f"{actual_provider} | model: {actual_model_detail}",
                        provider_reason=actual_provider_reason,
                        detail=(
                            f"vráceno do {project.status.value} | "
                            f"důvod: {project.stop_reason} | "
                            f"{comparison_detail} | {route_detail}"
                        ),
                    )
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
