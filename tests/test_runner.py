from datetime import timedelta

from ai_project_manager import slack_notify
from ai_project_manager.guard import OrchestratorGuard
from ai_project_manager.lock import ProjectLockManager
from ai_project_manager.models import DoDItem, ProjectRecord, ProjectStatus
from ai_project_manager.providers import ProviderRegistry
from ai_project_manager.runner import run_once, run_once_audit
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

    outcome = run_once(
        client, [project], registry, lambda _p, _pr: {"status": "done"}, default_providers=["claude"]
    )

    assert outcome.ran is True
    assert len(calls) == 2
    assert "Zahajuji" in calls[0] and "Demo" in calls[0]
    assert "Průběžný stav" in calls[1] and "audit" in calls[1].lower()


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
