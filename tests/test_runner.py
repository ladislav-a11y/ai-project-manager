from datetime import timedelta

from ai_project_manager import slack_notify
from ai_project_manager.guard import OrchestratorGuard
from ai_project_manager.lock import ProjectLockManager
from ai_project_manager.models import DoDItem, ProjectRecord, ProjectStatus
from ai_project_manager.providers import ProviderRegistry
from ai_project_manager.runner import (
    _capture_live_trello_readback,
    run_once,
    run_once_audit,
)
from ai_project_manager.trello_client import InMemoryTrelloClient
from ai_project_manager.trello_sync import build_list_maps, project_from_card, sync_project_to_trello


def make_client_with_project(project: ProjectRecord) -> InMemoryTrelloClient:
    client = InMemoryTrelloClient()
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]
    return client


def test_run_once_does_nothing_and_never_calls_run_fn_when_no_work():
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()  # no providers registered/available

    calls = []

    def run_fn(project, provider):
        calls.append((project.name, provider))
        return {}

    outcome = run_once(client, [project], registry, run_fn, default_providers=["claude"])

    assert outcome.ran is False
    assert calls == []


def test_run_once_executes_and_syncs_full_state_back_to_trello():
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    def run_fn(project, provider):
        return {
            "checkpoint": {"step": 1},
            "last_output": "did some work",
            "next_step": "do more work",
            "stop_reason": "provider session limit hit",
            "retry_after": "2026-01-01T00:30:00+00:00",
            "status": "paused",
        }

    outcome = run_once(client, [project], registry, run_fn, default_providers=["claude"])

    assert outcome.ran is True
    assert outcome.provider == "claude"

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.checkpoint == {"step": 1}
    assert reloaded.last_output == "did some work"
    assert reloaded.next_step == "do more work"
    assert reloaded.stop_reason == "provider session limit hit"
    assert reloaded.retry_after == "2026-01-01T00:30:00+00:00"
    assert reloaded.status == ProjectStatus.PAUSED
    assert reloaded.provider == "claude"


def test_run_once_releases_lock_and_reports_error_when_run_fn_raises():
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    lock_manager = ProjectLockManager()

    def run_fn(project, provider):
        raise RuntimeError("boom")

    outcome = run_once(client, [project], registry, run_fn, lock_manager=lock_manager, default_providers=["claude"])

    assert outcome.ran is True
    assert outcome.reason == "boom"
    assert lock_manager.is_locked("Demo") is False

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.stop_reason == "boom"


def test_run_once_skips_project_already_locked_by_another_holder():
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    lock_manager = ProjectLockManager(default_timeout=timedelta(minutes=15))
    lock_manager.acquire("Demo", "other-worker")

    calls = []

    def run_fn(project, provider):
        calls.append(project.name)
        return {}

    outcome = run_once(client, [project], registry, run_fn, lock_manager=lock_manager, default_providers=["claude"])

    assert outcome.ran is False
    assert calls == []


def test_run_once_runs_next_project_when_highest_priority_is_locked():
    busy = ProjectRecord(name="Busy", priority=5, status=ProjectStatus.READY)
    available = ProjectRecord(name="Available", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(busy)
    sync_project_to_trello(client, available)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    lock_manager = ProjectLockManager(default_timeout=timedelta(minutes=15))
    lock_manager.acquire("Busy", "other-worker")
    calls = []

    def run_fn(project, provider):
        calls.append((project.name, provider))
        return {"status": "done"}

    outcome = run_once(
        client,
        [busy, available],
        registry,
        run_fn,
        lock_manager=lock_manager,
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert outcome.project_name == "Available"
    assert calls == [("Available", "claude")]


def test_run_once_may_refresh_lock_owned_by_same_holder():
    project = ProjectRecord(name="Mine", priority=4, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    lock_manager = ProjectLockManager(default_timeout=timedelta(minutes=15))
    lock_manager.acquire("Mine", "project-manager")
    calls = []

    def run_fn(project, provider):
        calls.append(project.name)
        return {"status": "done"}

    outcome = run_once(
        client,
        [project],
        registry,
        run_fn,
        lock_manager=lock_manager,
        holder="project-manager",
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert calls == ["Mine"]
    assert lock_manager.is_locked("Mine") is False


def test_run_once_normalizes_null_checkpoint_from_external_result():
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.READY,
        checkpoint={"old": "resume point"},
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    outcome = run_once(
        client,
        [project],
        registry,
        lambda _project, _provider: {"status": "done", "checkpoint": None},
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert project.status == ProjectStatus.TESTING
    assert project.checkpoint == {}


def test_run_once_finalizes_before_promoting_to_testing():
    """Incident: card P5.20 (Station Agent - oprava P5, 2026-09-03). This is
    the path that actually fires the moment an implementation batch reports
    "done" within the SAME tick that dispatched it - daemon._promote_
    completed_implementations_to_testing's own finalize gate is a separate,
    later catch-up check and never runs for this case, so without a gate
    HERE too, the very first "implementation reported done" already lands
    the card in Testovani with nothing committed."""
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.READY,
        dod=[
            DoDItem(text="implementation", phase="implementation", checked=False),
            DoDItem(text="independent audit", phase="audit", checked=False),
        ],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    finalize_calls = []

    def finalize_fn(finalized_project):
        finalize_calls.append(finalized_project.name)
        return {
            "status": "done",
            "checkpoint": {"completed_dod_indices": [0], "finalization": {"done": True}},
        }

    outcome = run_once(
        client,
        [project],
        registry,
        lambda _project, _provider: {
            "status": "done",
            "checkpoint": {"completed_dod_indices": [0]},
        },
        default_providers=["claude"],
        finalize_fn=finalize_fn,
    )

    assert outcome.ran is True
    assert finalize_calls == ["Demo"]
    assert project.status == ProjectStatus.TESTING
    assert project.checkpoint.get("finalization") == {"done": True}


def test_run_once_keeps_card_in_progress_when_finalization_fails():
    """A dirty checkout without a verified commit must never reach
    Testovani - the audit would just reject it as unchanged. The card stays
    in Pracuje se with a concrete reason instead."""
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.READY,
        dod=[
            DoDItem(text="implementation", phase="implementation", checked=False),
            DoDItem(text="independent audit", phase="audit", checked=False),
        ],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    def finalize_fn(_project):
        return {"status": "blocked", "stop_reason": "tests failed: 2 failures"}

    outcome = run_once(
        client,
        [project],
        registry,
        lambda _project, _provider: {
            "status": "done",
            "checkpoint": {"completed_dod_indices": [0]},
        },
        default_providers=["claude"],
        finalize_fn=finalize_fn,
    )

    assert outcome.ran is True
    assert project.status == ProjectStatus.IN_PROGRESS
    assert "tests failed: 2 failures" in (project.stop_reason or "")


def test_run_once_records_non_mapping_external_result_as_a_run_failure():
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    outcome = run_once(
        client,
        [project],
        registry,
        lambda _project, _provider: None,
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert outcome.reason == "orchestrator result must be a mapping, got NoneType"
    assert project.stop_reason == outcome.reason
    assert client.get_card(project.trello_card_id)["desc"].find(outcome.reason) >= 0


def test_run_once_halts_project_after_repeated_identical_failures():
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    guard = OrchestratorGuard(max_repeats=1)

    def run_fn(project, provider):
        raise RuntimeError("permission denied: rm -rf /")

    run_once(client, [project], registry, run_fn, guard=guard, default_providers=["claude"])
    outcome = run_once(client, [project], registry, run_fn, guard=guard, default_providers=["claude"])

    assert outcome.halted is True

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status == ProjectStatus.BLOCKED
    assert "permission denied" in reloaded.blocked_by


def test_run_once_resets_recovery_attempts_once_project_is_no_longer_blocked():
    project = ProjectRecord(
        name="Demo", priority=3, status=ProjectStatus.IN_PROGRESS, recovery_attempts=3,
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    outcome = run_once(
        client, [project], registry, lambda _p, _pr: {"status": "done"}, default_providers=["claude"]
    )

    assert outcome.ran is True
    assert project.recovery_attempts == 0


def test_run_once_does_not_reset_recovery_attempts_when_run_immediately_re_blocks():
    """A run that reports "blocked" again for the same underlying reason
    must not wipe the unattended-recovery attempt counter (recovery.py) -
    doing so would let an unfixable block requeue forever, once per tick,
    defeating recovery's max-attempts loop guard."""
    project = ProjectRecord(
        name="Demo", priority=3, status=ProjectStatus.IN_PROGRESS, recovery_attempts=3,
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    outcome = run_once(
        client,
        [project],
        registry,
        lambda _p, _pr: {"status": "blocked", "stop_reason": "connection reset while calling the provider"},
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert project.recovery_attempts == 3


def test_generic_blocked_result_gets_actionable_remaining_step():
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.READY,
            dod=[DoDItem(text="hotovo", checked=True), DoDItem(text="doplnit konfiguraci")],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_once(
        client,
        [project],
        registry,
        lambda _p, _pr: {"status": "blocked", "stop_reason": "blocked"},
        default_providers=["claude"],
    )

    assert project.status == ProjectStatus.BLOCKED
    assert "doplnit konfiguraci" in project.stop_reason
    assert project.stop_reason != "blocked"


def test_run_once_refuses_to_close_card_with_unverified_dod_items():
    """A card must never land in Hotovo/DONE while any Definition-of-Done
    item is still unchecked, even if the orchestrator itself reported
    "done" - see run 97ff125c008d497989af07e068ae914e."""
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.READY,
        dod=[DoDItem(text="bod 1"), DoDItem(text="bod 2"), DoDItem(text="bod 3")],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    outcome = run_once(
        client,
        [project],
        registry,
        lambda _p, _pr: {"status": "done", "checkpoint": {"completed_dod_indices": [0, 1]}},
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert project.status != ProjectStatus.DONE
    assert "1" in project.stop_reason

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status != ProjectStatus.DONE
    assert [item.checked for item in reloaded.dod] == [True, True, False]


def test_run_once_closes_card_once_every_dod_item_is_verified():
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.READY,
        dod=[DoDItem(text="bod 1"), DoDItem(text="bod 2")],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    outcome = run_once(
        client,
        [project],
        registry,
        lambda _p, _pr: {"status": "done", "checkpoint": {"completed_dod_indices": [0, 1]}},
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert project.status == ProjectStatus.TESTING

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status == ProjectStatus.TESTING
    assert all(item.checked for item in reloaded.dod)


def test_run_once_never_contacts_slack_when_not_explicitly_enabled(monkeypatch):
    """Regression guard for false Slack notifications from test/verification
    runs (2026-08-26 incident): even with a webhook URL present in the
    environment - as a developer shell or verification script might inherit
    it - run_once() must not reach the network unless AI_PM_SLACK_ENABLED is
    explicitly set, matching production's opt-in wiring in
    scripts/run-ai-project-manager.ps1."""
    monkeypatch.delenv("AI_PM_SLACK_ENABLED", raising=False)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.invalid/should-not-be-used")
    calls = []
    monkeypatch.setattr(slack_notify.requests, "post", lambda *a, **k: calls.append((a, k)))

    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    outcome = run_once(
        client, [project], registry, lambda _p, _pr: {"status": "done"}, default_providers=["claude"]
    )

    assert outcome.ran is True
    assert calls == []


def test_run_once_notifies_slack_start_and_done_when_explicitly_enabled(monkeypatch):
    """Mirrors the real production opt-in: scripts/run-ai-project-manager.ps1
    sets both SLACK_WEBHOOK_URL and AI_PM_SLACK_ENABLED='1' before a real
    --once run. With that same wiring, a completed run must still announce
    both the start and the completion, so the isolation fix for test/
    verification runs does not silently break production notifications."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.invalid/prod")
    monkeypatch.setenv("AI_PM_SLACK_ENABLED", "1")
    calls = []

    class FakeResponse:
        status_code = 200

    def fake_post(*args, **kwargs):
        calls.append(kwargs.get("json", {}).get("text", ""))
        return FakeResponse()

    monkeypatch.setattr(slack_notify.requests, "post", fake_post)

    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    registry.configure_models("claude", ["claude-opus-4-1", "claude-sonnet-4"])

    outcome = run_once(
        client, [project], registry, lambda _p, _pr: {"status": "done"}, default_providers=["claude"]
    )

    assert outcome.ran is True
    assert len(calls) == 2
    assert "PM zahajuje práci" in calls[0] and "Demo" in calls[0]
    assert "proč: provider je první dostupný" in calls[0]
    assert "PM požaduje model claude-opus-4-1 pro implementaci" in calls[0]
    assert "Průběžný stav: PM ukončil tick" in calls[1] and "audit" in calls[1].lower()
    assert "total=n/a" in calls[1]


def test_run_once_reports_the_task_classification_model_tier(monkeypatch):
    """Real routing, not just design: the pre-call reason names the model
    quality tier task_classification.classify_task resolved for this card's
    complexity, not only the generic "provider picks per task type" text."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.invalid/prod")
    monkeypatch.setenv("AI_PM_SLACK_ENABLED", "1")
    calls = []

    class FakeResponse:
        status_code = 200

    def fake_post(*args, **kwargs):
        calls.append(kwargs.get("json", {}).get("text", ""))
        return FakeResponse()

    monkeypatch.setattr(slack_notify.requests, "post", fake_post)

    # No DoD items yet -> infer_complexity is "low" -> implementation's tier
    # rule maps that to "economical" (task_classification._MODEL_TIER_RULES).
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    outcome = run_once(
        client, [project], registry, lambda _p, _pr: {"status": "done"}, default_providers=["claude"]
    )

    assert outcome.ran is True
    assert "cílová úroveň economical pro náročnost low" in calls[0]


def test_run_once_slack_explains_actual_model_after_provider_failover(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.invalid/prod")
    monkeypatch.setenv("AI_PM_SLACK_ENABLED", "1")
    calls = []

    class FakeResponse:
        status_code = 200

    def fake_post(*args, **kwargs):
        calls.append(kwargs.get("json", {}).get("text", ""))
        return FakeResponse()

    monkeypatch.setattr(slack_notify.requests, "post", fake_post)

    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("antigravity")

    outcome = run_once(
        client,
        [project],
        registry,
        lambda _project, _provider: {
            "status": "done",
            "active_provider": "codex",
            "active_model": "gpt-5.6-luna",
            "provider_sequence": ["antigravity", "codex"],
        },
        default_providers=["antigravity", "codex"],
    )

    assert outcome.ran is True
    assert len(calls) == 2
    assert "codex | model: gpt-5.6-luna" in calls[1]
    assert "provider codex byl použit po failoveru z antigravity" in calls[1]
    assert "model gpt-5.6-luna je pro implementaci skutečně použitý model providera" in calls[1]
    assert project.extra_data["provider_selection"]["provider_reason"] in calls[1]
    # The requested policy decision (recorded before dispatch) must survive
    # unchanged next to the receipt fields written after the run returns, so
    # the requested antigravity/economical choice stays distinguishable from
    # the codex/gpt-5.6-luna receipt above.
    classification = project.extra_data["provider_selection"]["classification"]
    assert classification == {
        "task_type": "implementation",
        "complexity": "low",
        "model_tier": "economical",
        "forbidden_providers": [],
    }
    assert project.extra_data["provider_selection"]["selected_provider"] == "antigravity"
    assert project.extra_data["provider_selection"]["actual_provider"] == "codex"


def test_run_once_records_requested_classification_before_dispatch():
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    captured = {}

    def run_fn(_project, _provider):
        # The classification snapshot must already be present at dispatch
        # time, before the run's receipt fields (actual_provider/model)
        # exist at all.
        captured["classification"] = dict(_project.extra_data["provider_selection"]["classification"])
        return {"status": "done"}

    outcome = run_once(client, [project], registry, run_fn, default_providers=["claude"])

    assert outcome.ran is True
    assert captured["classification"] == {
        "task_type": "implementation",
        "complexity": "low",
        "model_tier": "economical",
        "forbidden_providers": [],
    }


def test_run_once_records_requested_model_but_preserves_missing_receipt_as_unknown():
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("antigravity")
    registry.configure_models("antigravity", ["requested-model"])

    outcome = run_once(
        client,
        [project],
        registry,
        lambda _project, _provider: {"status": "done"},
        default_providers=["antigravity"],
    )

    assert outcome.ran is True
    selection = project.extra_data["provider_selection"]
    assert selection["selected_model"] == "requested-model"
    assert selection["actual_model"] is None
    assert selection["model"] is None


def test_run_once_audit_is_the_only_path_to_hotovo():
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.TESTING,
        main_task="Implement and verify the feature",
        dod=[DoDItem(text="implementation", checked=True)],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    outcome = run_once_audit(
        client,
        [project],
        registry,
        lambda _project, _provider: {
            "verdict": "accepted",
            "evidence": "ai-orchestrator verified the implementation",
        },
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert project.status == ProjectStatus.DONE
    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status == ProjectStatus.DONE
    assert "ai-orchestrator verified" in reloaded.last_output
    assert reloaded.returned_from_testing is False


def test_run_once_audit_requests_quality_model_from_verified_catalog():
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.TESTING,
        main_task="Implement and verify the feature",
        dod=[DoDItem(text="implementation", checked=True)],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    registry.configure_models("claude", ["claude-sonnet-4", "claude-opus-4-1"])

    outcome = run_once_audit(
        client,
        [project],
        registry,
        lambda _project, _provider: {
            "verdict": "accepted",
            "evidence": "ai-orchestrator verified the implementation",
        },
        default_providers=["claude"],
    )

    assert outcome.ran is True
    selection = project.extra_data["provider_selection"]
    assert selection["selected_model"] == "claude-opus-4-1"
    assert selection["actual_model"] is None


def test_run_once_audit_records_requested_classification_next_to_receipt():
    """Audit always targets the quality tier regardless of complexity - the
    stored classification snapshot (requested) must reflect that, sitting
    alongside the actual_provider/actual_model receipt fields written once
    the verdict comes back."""
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.TESTING,
        main_task="Implement and verify the feature",
        dod=[DoDItem(text="implementation", checked=True)],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    outcome = run_once_audit(
        client,
        [project],
        registry,
        lambda _project, _provider: {
            "verdict": "accepted",
            "evidence": "ai-orchestrator verified the implementation",
            "active_provider": "claude",
            "active_model": "claude-opus-4-1",
        },
        default_providers=["claude"],
    )

    assert outcome.ran is True
    selection = project.extra_data["provider_selection"]
    assert selection["classification"] == {
        "task_type": "audit",
        "complexity": "low",
        "model_tier": "quality",
        "forbidden_providers": [],
    }
    assert selection["actual_provider"] == "claude"
    assert selection["actual_model"] == "claude-opus-4-1"


def test_implementation_receipt_preserves_catalog_for_following_audit_dispatch():
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.READY,
        main_task="Implement and verify the feature",
        dod=[DoDItem(text="implementation", checked=False)],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    registry.configure_models("claude", ["claude-sonnet-4", "claude-opus-4-1"])

    implementation = run_once(
        client,
        [project],
        registry,
        lambda _project, _provider: {
            "status": "done",
            "active_provider": "claude",
            "active_model": "claude-sonnet-4",
            "checkpoint": {"completed_dod_indices": [0]},
        },
        default_providers=["claude"],
    )

    assert implementation.ran is True
    assert project.status == ProjectStatus.TESTING
    assert registry.get_status("claude").models == (
        "claude-sonnet-4",
        "claude-opus-4-1",
    )

    dispatched = []
    audit = run_once_audit(
        client,
        [project],
        registry,
        lambda audit_project, provider: dispatched.append(
            (audit_project.extra_data["provider_selection"]["model"], provider)
        )
        or {
            "verdict": "accepted",
            "evidence": "ai-orchestrator verified the implementation",
            "active_provider": "claude",
            "active_model": "claude-opus-4-1",
        },
        default_providers=["claude"],
    )

    assert audit.ran is True
    assert dispatched == [(None, "claude")]


def test_run_once_audit_rejected_returns_concrete_feedback_to_pracuje_se():
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.TESTING,
        main_task="Implement and verify the feature",
        dod=[DoDItem(text="implementation", checked=True)],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    outcome = run_once_audit(
        client,
        [project],
        registry,
        lambda _project, _provider: {
            "verdict": "rejected",
            "reason": "export still times out on large accounts",
            "evidence": "ran the export against a 10k-row fixture, it timed out after 30s",
        },
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert project.status == ProjectStatus.IN_PROGRESS
    assert project.returned_from_testing is True
    assert "export still times out" in project.stop_reason
    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status == ProjectStatus.IN_PROGRESS
    assert any("export still times out" in item for item in reloaded.open_feedback)
    assert any("Evidence:" in item for item in reloaded.open_feedback)


def test_run_once_audit_records_provider_capability_limit_for_plan_without_verdict():
    project = ProjectRecord(
        name="P5.04 — propagation a scoring",
        project_key="Station Agent",
        priority=5.04,
        status=ProjectStatus.TESTING,
        main_task="Implement and verify the feature",
        dod=[DoDItem(text="implementation", checked=True)],
        extra_data={"inbox_preparation": {"scope": "propagation a scoring"}},
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("antigravity")

    outcome = run_once_audit(
        client,
        [project],
        registry,
        lambda _project, _provider: {
            "verdict": "rejected",
            "reason": "needs verification",
            "evidence": "pending audit verdict; no live verification was performed",
        },
        default_providers=["antigravity"],
    )

    assert outcome.ran is True
    assert registry.is_capability_limited(
        "antigravity", "audit:station agent:propagation a scoring"
    )


def test_run_once_audit_rejected_with_explicit_reject_target_ready():
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.TESTING,
        dod=[DoDItem(text="implementation", checked=True)],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    outcome = run_once_audit(
        client,
        [project],
        registry,
        lambda _project, _provider: {
            "verdict": "rejected",
            "reason": "approach is unsalvageable, start over",
            "evidence": "tried three fixes, all regressed the same test",
            "reject_target": "ready",
        },
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert project.status == ProjectStatus.READY
    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status == ProjectStatus.READY


def test_run_once_audit_rejected_audit_only_stays_in_testing_and_persists_readback():
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.TESTING,
        main_task="Implement and verify the feature",
        dod=[
            DoDItem(text="implementation", checked=True),
            DoDItem(text="independent audit", phase="audit"),
        ],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    seen = {}

    def audit_run(project, _provider):
        seen["readback"] = project.extra_data["live_trello_readback"]
        return {
            "verdict": "rejected",
            "reason": "fresh live readback was not accepted",
            "evidence": "auditor checked the current card and requested another audit pass",
            "reject_target": "testing",
        }

    outcome = run_once_audit(
        client,
        [project],
        registry,
        audit_run,
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert project.status == ProjectStatus.TESTING
    assert project.returned_from_testing is False
    assert seen["readback"]["status"] == "ok"
    assert seen["readback"]["card_id"] == project.trello_card_id
    assert seen["readback"]["list_name"] == "Testing"
    assert seen["readback"]["dod"][1]["phase"] == "audit"
    assert "fresh live readback was not accepted" in project.stop_reason
    assert "live_trello_readback" in project.extra_data


def test_audit_readback_preserves_live_provider_model_and_slack_receipt():
    project = ProjectRecord(
        name="P5.01 — oprava PM model selection",
        priority=5.01,
        status=ProjectStatus.READY,
        main_task="Implement and verify model selection",
        dod=[DoDItem(text="implementation", checked=True)],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("codex")

    implementation = run_once(
        client,
        [project],
        registry,
        lambda _project, _provider: {
            "status": "done",
            "run_id": "implementation-live-run",
            "active_provider": "codex",
            "active_model": "gpt-5.6-luna",
            "provider_sequence": ["codex"],
        },
        default_providers=["codex"],
    )
    assert implementation.ran is True
    assert project.status == ProjectStatus.TESTING

    seen = {}

    def audit_run(audit_project, _provider):
        seen["readback"] = audit_project.extra_data["live_trello_readback"]
        return {
            "verdict": "rejected",
            "reason": "audit evidence intentionally withheld by test",
            "evidence": "readback was inspected",
            "reject_target": "testing",
        }

    outcome = run_once_audit(
        client,
        [project],
        registry,
        audit_run,
        default_providers=["codex"],
    )

    assert outcome.ran is True
    metadata = seen["readback"]["contract_metadata"]
    evidence = metadata["provider_selection_history"][0]["live_evidence"]
    assert evidence["active_model"] == "gpt-5.6-luna"
    assert evidence["active_provider"] == "codex"
    assert evidence["run_id"] == "implementation-live-run"


def test_audit_readback_preserves_inbox_split_priority_identity_dependency_and_workflow_state():
    inbox_preparation = {
        "source_card_id": "source-card-1",
        "source_card_url": "https://trello.example/c/source-card-1",
        "content_sha256": "a" * 64,
        "subtask_index": 1,
        "subtask_count": 2,
        "scope": "rozšíření",
        "source_priority": 3,
        "task_priority": 5,
        "depends_on_subtask_indices": [0],
        "execution_order": 1,
        "project_path": "/repo/demo",
        "generated_project": False,
    }
    project = ProjectRecord(
        name="Demo — požadavek 2",
        priority=5,
        status=ProjectStatus.TESTING,
        main_task="Implement and verify the split subtask",
        dod=[DoDItem(text="implementation", checked=True)],
        extra_data={"inbox_preparation": dict(inbox_preparation)},
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    seen = {}

    def audit_run(audit_project, _provider):
        seen["readback"] = audit_project.extra_data["live_trello_readback"]
        return {
            "verdict": "rejected",
            "reason": "audit evidence intentionally withheld by test",
            "evidence": "readback was inspected",
            "reject_target": "testing",
        }

    outcome = run_once_audit(
        client,
        [project],
        registry,
        audit_run,
        default_providers=["claude"],
    )

    assert outcome.ran is True
    # The readback is captured fresh from the live Trello card, i.e. after a
    # full sync-to-Trello -> project_from_card round trip and Card Contract
    # migration, not merely echoed from the in-memory ProjectRecord.
    assert seen["readback"]["contract_metadata"]["inbox_preparation"] == inbox_preparation


def test_capture_live_trello_readback_reflects_persisted_card_not_stale_in_memory_claim():
    """An auditor must never have to trust the agent's in-memory claim.

    Write the correct priority/identity/dependency/workflow-state data to
    Trello, then corrupt the in-memory ProjectRecord *after* that write (as a
    stale snapshot or a bug would). The readback handed to the audit must
    still match what is actually persisted on the card - proving it is
    produced by fetching and re-parsing the live card, not by echoing
    ``project.extra_data``/``project.dod``/``project.checkpoint``.
    """
    inbox_preparation = {
        "source_card_id": "source-card-9",
        "source_card_url": "https://trello.example/c/source-card-9",
        "content_sha256": "b" * 64,
        "subtask_index": 2,
        "subtask_count": 3,
        "scope": "rozšíření",
        "source_priority": 4,
        "task_priority": 2,
        "depends_on_subtask_indices": [0, 1],
        "execution_order": 2,
        "project_path": "/repo/demo2",
        "generated_project": False,
    }
    project = ProjectRecord(
        name="Demo — požadavek 4",
        priority=2,
        status=ProjectStatus.TESTING,
        main_task="Implement and verify the split subtask",
        dod=[DoDItem(text="implementation", checked=True)],
        checkpoint={"step": 3},
        extra_data={"inbox_preparation": dict(inbox_preparation)},
    )
    client = make_client_with_project(project)

    # Tamper with the in-memory claim only - the persisted card is untouched.
    project.extra_data["inbox_preparation"] = {
        **inbox_preparation,
        "depends_on_subtask_indices": [],
        "task_priority": 999,
    }
    project.priority = 999
    project.checkpoint = {"step": 999}

    readback = _capture_live_trello_readback(client, project)

    assert readback["status"] == "ok"
    assert readback["contract_metadata"]["inbox_preparation"] == inbox_preparation
    assert readback["priority"] == 2
    assert readback["checkpoint"] == {"step": 3}


def test_run_once_audit_moves_provider_limit_to_waiting_phase():
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.TESTING,
        checkpoint={"step": 4},
        dod=[DoDItem(text="implementation", checked=True)],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    outcome = run_once_audit(
        client,
        [project],
        registry,
        lambda _project, _provider: {
            "status": "paused",
            "stop_reason": "provider session limit hit",
            "retry_after": "2026-01-01T00:30:00+00:00",
            "checkpoint": {"step": 5},
        },
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert project.status == ProjectStatus.PAUSED
    assert project.extra_data["resume_status"] == ProjectStatus.TESTING.value
    assert project.checkpoint == {"step": 5}
    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status == ProjectStatus.PAUSED
    assert reloaded.extra_data["resume_status"] == ProjectStatus.TESTING.value


def test_run_once_audit_marks_provider_error_and_preserves_testing_on_missing_verdict():
    project = ProjectRecord(
        name="Audit provider error",
        priority=3,
        status=ProjectStatus.TESTING,
        checkpoint={"completed_dod_indices": [0]},
        dod=[DoDItem(text="implementation", checked=True)],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("antigravity")

    outcome = run_once_audit(
        client,
        [project],
        registry,
        lambda _project, _provider: (_ for _ in ()).throw(
            RuntimeError("agy nemůže otevřít installation_id")
        ),
        default_providers=["antigravity", "codex"],
    )

    assert outcome.ran is True
    assert project.status == ProjectStatus.TESTING
    assert "installation_id" in project.stop_reason
    assert registry.get_status("antigravity").state == "ERROR"
    assert registry.get_status("antigravity").retry_after is not None


def test_run_once_audit_returns_incomplete_testing_card_to_work_without_ai_call():
    project = ProjectRecord(
        name="Incomplete audit",
        priority=3,
        status=ProjectStatus.TESTING,
        dod=[
            DoDItem(text="already verified", checked=True),
            DoDItem(text="still needs implementation", checked=False),
        ],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    calls = []

    outcome = run_once_audit(
        client,
        [project],
        registry,
        lambda *_args: calls.append(True),
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert project.status == ProjectStatus.IN_PROGRESS
    assert project.returned_from_testing is True
    assert calls == []
    assert "DoD bodů ještě není ověřeno" in project.stop_reason


def test_run_once_audit_finalizes_before_dispatching_when_not_yet_verified():
    """Defense in depth for the incident that got this feature written:
    card P5.20 (Station Agent - oprava P5, 2026-09-03) reached Testovani
    with its implementation DoD complete but never committed, because the
    path that promoted it (runner._apply_run_result, same tick as the
    implementation dispatch) did not yet call finalize_fn. Even after that
    gap is closed, a card can still reach Testovani unfinalized through some
    other path (an older stuck card, a future code change) - this check
    catches it right before spending a real audit-provider call on a
    checkout the audit would just find unchanged."""
    project = ProjectRecord(
        name="Stuck in testing",
        priority=5,
        status=ProjectStatus.TESTING,
        dod=[
            DoDItem(text="implementation complete", checked=True),
            DoDItem(text="independent audit", phase="audit"),
        ],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    finalize_calls = []
    audit_calls = []

    def finalize_fn(finalized_project):
        finalize_calls.append(finalized_project.name)
        return {
            "status": "done",
            "checkpoint": {"finalization": {"done": True}},
        }

    def audit_run_fn(audited_project, _provider):
        audit_calls.append(True)
        return {"verdict": "accepted", "evidence": "audit passed"}

    outcome = run_once_audit(
        client,
        [project],
        registry,
        audit_run_fn,
        default_providers=["claude"],
        finalize_fn=finalize_fn,
    )

    assert outcome.ran is True
    assert finalize_calls == ["Stuck in testing"]
    assert audit_calls == [True]
    assert project.checkpoint.get("finalization") == {"done": True}


def test_run_once_audit_returns_unfinalized_card_to_work_without_spending_audit_call():
    project = ProjectRecord(
        name="Stuck in testing",
        priority=5,
        status=ProjectStatus.TESTING,
        dod=[
            DoDItem(text="implementation complete", checked=True),
            DoDItem(text="independent audit", phase="audit"),
        ],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    audit_calls = []

    def finalize_fn(_project):
        return {"status": "blocked", "stop_reason": "tests failed: 2 failures"}

    def audit_run_fn(*_args):
        audit_calls.append(True)
        return {"verdict": "accepted", "evidence": "audit passed"}

    outcome = run_once_audit(
        client,
        [project],
        registry,
        audit_run_fn,
        default_providers=["claude"],
        finalize_fn=finalize_fn,
    )

    assert outcome.ran is True
    assert audit_calls == []
    assert project.status == ProjectStatus.IN_PROGRESS
    assert "tests failed: 2 failures" in (project.stop_reason or "")


def test_token_waste_audit_only_card_never_calls_implementation_agent():
    project = ProjectRecord(
        name="Audit only",
        priority=5,
        status=ProjectStatus.TESTING,
        main_task="Already implemented",
        dod=[
            DoDItem(text="implementation complete", checked=True),
            DoDItem(text="independent audit", phase="audit"),
        ],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    implementation_calls = []

    outcome = run_once(
        client,
        [project],
        registry,
        lambda *_args: implementation_calls.append(True),
        default_providers=["claude"],
    )

    assert outcome.ran is False
    assert implementation_calls == []
    assert project.status == ProjectStatus.TESTING
