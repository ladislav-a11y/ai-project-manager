import logging
from datetime import datetime, timedelta, timezone

import pytest

from ai_project_manager.daemon import (
    _run_recovery_pass,
    _resume_due_provider_waits,
    load_projects_and_inbox,
    recheck_due_providers,
    run_loop,
    run_tick,
)
from ai_project_manager.recovery import default_backoff
from ai_project_manager.lock import ProjectLockManager
from ai_project_manager.models import DoDItem, ProjectRecord, ProjectStatus
from ai_project_manager.providers import ProviderRegistry, ProviderState
from ai_project_manager.provider_state import load_provider_state
from ai_project_manager.recovery import DEFAULT_MAX_ATTEMPTS
from ai_project_manager.trello_client import InMemoryTrelloClient
from ai_project_manager.trello_sync import build_list_maps, project_from_card, sync_project_to_trello


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    # run_tick/run_loop persist provider state to a relative
    # "provider_state.json" by default; chdir into a throwaway directory
    # so that write never lands in (and pollutes) the real repo checkout.
    monkeypatch.chdir(tmp_path)


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def make_client_with_project(project: ProjectRecord) -> InMemoryTrelloClient:
    client = InMemoryTrelloClient()
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]
    return client


def test_run_tick_does_no_ai_call_when_no_schedulable_work():
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()  # nothing registered/available

    calls = []

    def run_fn(project, provider):
        calls.append((project.name, provider))
        return {}

    outcome = run_tick(client, registry, run_fn, default_providers=["claude"])

    assert outcome.ran is False
    assert calls == []


def test_run_tick_does_no_ai_call_when_only_provider_is_limited():
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    registry = ProviderRegistry(clock=clock)
    registry.mark_limited("claude", retry_after=timedelta(minutes=30))

    calls = []

    def run_fn(project, provider):
        calls.append((project.name, provider))
        return {}

    outcome = run_tick(client, registry, run_fn, default_providers=["claude"])

    assert outcome.ran is False
    assert calls == []


def test_run_tick_resumes_due_implementation_wait_before_testing():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    waiting = ProjectRecord(
        name="Resumed implementation",
        priority=3,
        status=ProjectStatus.PAUSED,
        checkpoint={"step": 5},
        provider="claude",
        stop_reason="provider session limit hit",
        retry_after=(clock.now - timedelta(minutes=1)).isoformat(),
        dod=[DoDItem(text="implementation", checked=False)],
    )
    testing = ProjectRecord(
        name="Pending audit",
        priority=5,
        status=ProjectStatus.TESTING,
        dod=[DoDItem(text="implementation", checked=True)],
    )
    client = InMemoryTrelloClient()
    sync_project_to_trello(client, waiting)
    sync_project_to_trello(client, testing)
    registry = ProviderRegistry(clock=clock)
    registry.mark_available("claude")
    calls = []

    def run_fn(project, provider):
        calls.append(("implementation", project.name, provider))
        return {"status": "in_progress"}

    def audit_run_fn(project, provider):
        calls.append(("audit", project.name, provider))
        return {"verdict": "accepted", "evidence": "audit passed"}

    outcome = run_tick(
        client,
        registry,
        run_fn,
        audit_run_fn=audit_run_fn,
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert calls == [("implementation", "Resumed implementation", "claude")]


def test_run_tick_promotes_completed_implementation_before_audit():
    project = ProjectRecord(
        name="Completed implementation",
        priority=5,
        status=ProjectStatus.IN_PROGRESS,
        main_task="Implement and verify the feature",
        dod=[DoDItem(text="implementation", checked=True)],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    implementation_calls = []
    audit_phases = []

    def run_fn(*_args):
        implementation_calls.append(True)
        return {"status": "in_progress"}

    def audit_run_fn(project, _provider):
        audit_phases.append(project.status)
        return {"verdict": "accepted", "evidence": "audit passed"}

    outcome = run_tick(
        client,
        registry,
        run_fn,
        audit_run_fn=audit_run_fn,
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert implementation_calls == []
    assert audit_phases == [ProjectStatus.TESTING]
    id_to_name, _ = build_list_maps(client)
    assert project_from_card(client.get_card(project.trello_card_id), id_to_name).status == ProjectStatus.DONE


def test_run_tick_promotes_complete_implementation_with_stale_return_marker():
    project = ProjectRecord(
        name="Historical audit return",
        priority=5,
        status=ProjectStatus.IN_PROGRESS,
        main_task="Implement and verify the feature",
        dod=[DoDItem(text="implementation", checked=True)],
        extra_data={"returned_from_testing": True},
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    implementation_calls = []
    audit_phases = []

    def run_fn(*_args):
        implementation_calls.append(True)
        return {"status": "in_progress"}

    def audit_run_fn(project, _provider):
        audit_phases.append(project.status)
        return {"verdict": "accepted", "evidence": "audit passed"}

    outcome = run_tick(
        client,
        registry,
        run_fn,
        audit_run_fn=audit_run_fn,
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert implementation_calls == []
    assert audit_phases == [ProjectStatus.TESTING]


def _implementation_plus_audit_dod(implementation_checked: bool = True) -> list[DoDItem]:
    """The real Inbox-prepared DoD shape (see inbox_preparation.py): one
    implementation item plus outstanding audit items. This is what keeps
    ``maintain_board_contract``'s own "every DoD item checked" early-promote
    shortcut (trello_sync.py) from firing before daemon.py's own gate ever
    runs - a plain card is never all-checked until the audit items are too,
    so that shortcut only ever fires for the implementation-only DoD shape
    these tests must NOT use."""
    return [
        DoDItem(text="implementation", phase="implementation", checked=implementation_checked),
        DoDItem(text="independent audit: tests", phase="audit", checked=False),
        DoDItem(text="independent audit: verdict", phase="audit", checked=False),
    ]


def test_run_tick_finalizes_before_promoting_completed_implementation():
    """A plain implementation DoD item never asks the controller finalizer
    to run by name (see orchestrator_runner._finalization_indices), so
    without an automatic finalize_fn call here nothing would ever be
    committed and every audit would find an unchanged checkout - the
    "checkout se nezmenil" rejection loop this was written to fix."""
    project = ProjectRecord(
        name="Completed implementation",
        priority=5,
        status=ProjectStatus.IN_PROGRESS,
        main_task="Implement and verify the feature",
        dod=_implementation_plus_audit_dod(),
        checkpoint={"run_id": "abc"},
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    finalize_calls = []
    finalized = {"done": False}

    def run_fn(*_args):
        raise AssertionError("implementation must not be dispatched again")

    def finalize_fn(finalized_project):
        # Mirrors the real build_finalize_fn's short-circuit for a HEAD
        # already covered by a verified proof - run_once_audit's own
        # defense-in-depth check (see incident: P5.20, Station Agent -
        # oprava P5) calls finalize_fn again right before dispatching the
        # audit, and that second call must be cheap/idempotent in
        # production, not a second real commit.
        finalize_calls.append(finalized_project.name)
        if finalized["done"]:
            return {"status": "done", "already_verified": True}
        finalized["done"] = True
        return {
            "status": "done",
            "checkpoint": {"run_id": "abc", "finalization": {"done": True}},
        }

    def audit_run_fn(audited_project, _provider):
        assert audited_project.status == ProjectStatus.TESTING
        return {"verdict": "accepted", "evidence": "audit passed"}

    outcome = run_tick(
        client,
        registry,
        run_fn,
        audit_run_fn=audit_run_fn,
        finalize_fn=finalize_fn,
        default_providers=["claude"],
    )

    assert outcome.ran is True
    # daemon._promote_completed_implementations_to_testing calls finalize_fn
    # once before promotion, then run_once_audit's own defense-in-depth
    # check calls it again right before dispatching the audit - both are
    # expected (see the finalize_fn fake above), never zero.
    assert finalize_calls == ["Completed implementation"] * len(finalize_calls)
    assert finalize_calls
    id_to_name, _ = build_list_maps(client)
    card = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert card.checkpoint.get("finalization") == {"done": True}


def test_run_tick_blocks_promotion_when_finalization_fails():
    """A dirty checkout without a verified commit must never reach
    Testování - the audit would just reject it as unchanged and burn a
    token cycle for nothing. The card stays in Pracuje se with a concrete
    reason instead."""
    project = ProjectRecord(
        name="Completed implementation",
        priority=5,
        status=ProjectStatus.IN_PROGRESS,
        main_task="Implement and verify the feature",
        dod=_implementation_plus_audit_dod(),
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    audit_calls = []

    def run_fn(*_args):
        raise AssertionError(
            "nothing left to implement - scheduler must not re-dispatch"
        )

    def finalize_fn(_project):
        return {"status": "blocked", "stop_reason": "tests failed: 2 failures"}

    def audit_run_fn(*_args):
        audit_calls.append(True)
        return {"verdict": "accepted", "evidence": "audit passed"}

    outcome = run_tick(
        client,
        registry,
        run_fn,
        audit_run_fn=audit_run_fn,
        finalize_fn=finalize_fn,
        default_providers=["claude"],
    )

    assert outcome.ran is False
    assert audit_calls == []
    id_to_name, _ = build_list_maps(client)
    card = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert card.status == ProjectStatus.IN_PROGRESS
    assert "tests failed: 2 failures" in (card.stop_reason or "")


def test_run_tick_loads_real_projects_and_processes_inbox_only_when_explicitly_enabled():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    existing = ProjectRecord(
        name="Dashboard",
        priority=2,
        status=ProjectStatus.READY,
        main_task="React dashboard for orchestrator status",
    )
    sync_project_to_trello(client, existing)
    client.create_card(
        name_to_id["Inbox"],
        "Dashboard spinner bug",
        desc="The orchestrator dashboard spinner never stops, this is a bug",
    )

    registry = ProviderRegistry()
    registry.mark_available("claude")

    calls = []

    def run_fn(project, provider):
        calls.append(project.name)
        return {"status": "in_progress", "last_output": "worked on it"}

    outcome = run_tick(
        client,
        registry,
        run_fn,
        default_providers=["claude"],
        process_inbox_enabled=True,
    )

    assert outcome.ran is True
    assert calls == ["Dashboard"]

    # A governed card already exists, so this tick must not spend an AI call
    # on Inbox intake. The source remains queued for a later idle tick.
    inbox_cards = client.list_cards(name_to_id["Inbox"])
    assert len(inbox_cards) == 1
    assert inbox_cards[0]["name"] == "Dashboard spinner bug"
    assert client.list_cards(name_to_id["Done"]) == []


def test_run_tick_keeps_new_inbox_task_in_ready_until_next_tick():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    client.create_card(
        name_to_id["Inbox"],
        "New dashboard feature",
        desc="Přidat novou funkci dashboardu.",
        labels=["AI Project Manager"],
    )

    registry = ProviderRegistry()
    registry.mark_available("claude")
    calls = []

    def run_fn(project, provider):
        calls.append(project.name)
        return {"status": "in_progress"}

    outcome = run_tick(
        client,
        registry,
        run_fn,
        default_providers=["claude"],
        process_inbox_enabled=True,
    )

    assert outcome.ran is False
    assert calls == []
    ready_cards = client.list_cards(name_to_id["New"])
    assert len(ready_cards) == 1
    assert ready_cards[0]["name"].startswith("P3 — ")
    assert {label["name"] for label in ready_cards[0]["labels"]} >= {"P3", "AI Project Manager"}


def test_run_tick_ignores_inbox_by_default():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    existing = ProjectRecord(
        name="Dashboard",
        priority=2,
        status=ProjectStatus.READY,
        main_task="React dashboard for orchestrator status",
    )
    sync_project_to_trello(client, existing)
    source = client.create_card(
        name_to_id["Inbox"],
        "Dashboard spinner bug",
        desc="The orchestrator dashboard spinner never stops, this is a bug",
    )

    registry = ProviderRegistry()
    registry.mark_available("claude")
    calls = []

    def run_fn(project, provider):
        calls.append(project.name)
        return {"status": "in_progress"}

    outcome = run_tick(client, registry, run_fn, default_providers=["claude"])

    assert outcome.ran is True
    assert calls == ["Dashboard"]
    assert [card["id"] for card in client.list_cards(name_to_id["Inbox"])] == [source["id"]]
    assert client.list_cards(name_to_id["Done"]) == []


def test_run_loop_once_runs_a_single_tick_and_never_sleeps():
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    sleep_calls = []

    def run_fn(project, provider):
        return {"status": "in_progress"}

    outcome = run_loop(
        client, registry, run_fn,
        once=True,
        sleep=lambda s: sleep_calls.append(s),
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert sleep_calls == []


def test_run_loop_sleeps_without_ai_call_when_no_work_and_stops_at_max_iterations():
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.DONE)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    calls = []
    sleep_calls = []

    def run_fn(project, provider):
        calls.append(project.name)
        return {}

    outcome = run_loop(
        client, registry, run_fn,
        once=False,
        poll_interval_seconds=42,
        sleep=lambda s: sleep_calls.append(s),
        default_providers=["claude"],
        max_iterations=3,
    )

    assert outcome.ran is False
    assert calls == []
    # 3 ticks run, but the loop returns as soon as max_iterations is hit,
    # before sleeping again - so only 2 sleeps happen in between.
    assert sleep_calls == [42, 42]


def test_run_loop_recovers_from_transient_tick_failure_and_retries():
    class FlakyClient(InMemoryTrelloClient):
        def __init__(self):
            super().__init__()
            self.list_attempts = 0

        def list_lists(self):
            self.list_attempts += 1
            if self.list_attempts == 1:
                raise RuntimeError("temporary Trello outage")
            return super().list_lists()

    client = FlakyClient()
    registry = ProviderRegistry()
    registry.mark_available("claude")
    sleeps = []

    outcome = run_loop(
        client,
        registry,
        lambda project, provider: {},
        poll_interval_seconds=11,
        sleep=sleeps.append,
        default_providers=["claude"],
        max_iterations=2,
    )

    # One call fails in the first tick; the successful retry reads the
    # board once for projects and once more to locate the Inbox list.
    assert client.list_attempts >= 3
    assert sleeps == [11]
    assert outcome.operational_error is False
    assert outcome.ran is False


def test_run_loop_once_reports_tick_failure_without_raising():
    class BrokenClient(InMemoryTrelloClient):
        def list_lists(self):
            raise RuntimeError("Trello unavailable")

    registry = ProviderRegistry()
    registry.mark_available("claude")

    outcome = run_loop(
        BrokenClient(),
        registry,
        lambda project, provider: {},
        once=True,
        default_providers=["claude"],
    )

    assert outcome.ran is False
    assert outcome.operational_error is True
    assert "Trello unavailable" in outcome.reason


def test_run_loop_auto_resumes_project_after_retry_after_without_manual_intervention():
    # A provider-limit stop is persisted in Čeká na AI and resumes from the
    # same checkpoint only after retry_after has elapsed.
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.PAUSED,
        checkpoint={"step": 5},
        provider="claude",
        stop_reason="provider session limit hit",
        retry_after=(clock.now + timedelta(minutes=30)).isoformat(),
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry(clock=clock)
    registry.mark_limited("claude", retry_after=timedelta(minutes=30), checkpoint={"step": 5})

    calls = []

    def run_fn(project, provider):
        calls.append((project.name, project.checkpoint))
        return {"status": "in_progress", "checkpoint": {"step": 6}}

    # First tick: still within the limit window, provider stays LIMITED,
    # no AI call is made and the project remains untouched.
    clock.advance(timedelta(minutes=10))
    outcome_1 = run_tick(client, registry, run_fn, default_providers=["claude"])
    assert outcome_1.ran is False
    assert calls == []

    # Second tick: retry_after has passed. The provider is rechecked for
    # free and the paused project is returned to Připraveno, then resumes
    # automatically from its checkpoint.
    clock.advance(timedelta(minutes=25))
    outcome_2 = run_tick(client, registry, run_fn, default_providers=["claude"])

    assert outcome_2.ran is True
    assert calls == [("Demo", {"step": 5})]
    assert registry.get_status("claude").state == ProviderState.AVAILABLE


def test_due_audit_provider_wait_returns_to_testing_not_ready():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    project = ProjectRecord(
        name="Audit demo",
        priority=3,
        status=ProjectStatus.PAUSED,
        checkpoint={"step": 5},
        provider="claude",
        stop_reason="provider session limit hit",
        retry_after=(clock.now + timedelta(minutes=30)).isoformat(),
        extra_data={"resume_status": ProjectStatus.TESTING.value},
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry(clock=clock)
    registry.mark_available("claude")
    clock.advance(timedelta(minutes=31))

    resumed = _resume_due_provider_waits(client, [project], registry)

    assert resumed == ["Audit demo"]
    assert project.status == ProjectStatus.TESTING
    assert project.retry_after is None
    assert "resume_status" not in project.extra_data
    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status == ProjectStatus.TESTING


def test_provider_wait_fails_over_to_available_provider_before_retry_deadline():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    project = ProjectRecord(
        name="Audit failover demo",
        priority=3,
        status=ProjectStatus.PAUSED,
        checkpoint={"completed_dod_indices": [0]},
        provider="claude",
        stop_reason="provider session limit hit",
        retry_after=(clock.now + timedelta(days=1)).isoformat(),
        extra_data={"resume_status": ProjectStatus.TESTING.value},
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry(clock=clock)
    registry.mark_limited("claude", retry_after=timedelta(days=1), checkpoint=project.checkpoint)
    registry.mark_available("antigravity")

    resumed = _resume_due_provider_waits(
        client,
        [project],
        registry,
        default_providers=["claude", "antigravity", "codex"],
    )

    assert resumed == ["Audit failover demo"]
    assert project.status == ProjectStatus.TESTING
    assert project.provider == "antigravity"
    assert project.retry_after is None
    assert project.checkpoint == {"completed_dod_indices": [0]}
    assert "resume_status" not in project.extra_data


def test_run_tick_only_requeues_resumed_audit_wait_without_same_tick_ai_call():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    project = ProjectRecord(
        name="Audit wait",
        priority=3,
        status=ProjectStatus.PAUSED,
        checkpoint={"completed_dod_indices": [0]},
        provider="hermes",
        stop_reason="provider session limit hit",
        retry_after=(clock.now + timedelta(days=1)).isoformat(),
        extra_data={"resume_status": ProjectStatus.TESTING.value},
        dod=[DoDItem(text="implemented", checked=True)],
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry(clock=clock)
    registry.mark_limited("hermes", retry_after=timedelta(days=1), checkpoint=project.checkpoint)
    registry.mark_available("antigravity")
    calls = []

    outcome = run_tick(
        client,
        registry,
        lambda *_args: calls.append("implementation"),
        default_providers=["hermes", "antigravity"],
        audit_run_fn=lambda *_args: calls.append("audit"),
    )

    assert outcome.ran is False
    assert calls == []
    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status == ProjectStatus.TESTING
    assert reloaded.provider == "antigravity"
    assert reloaded.retry_after is None
    assert "čekání na providera skončilo" in reloaded.stop_reason


def test_recheck_due_providers_returns_names_that_resumed():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    registry = ProviderRegistry(clock=clock)
    registry.mark_limited("claude", retry_after=timedelta(minutes=30))
    clock.advance(timedelta(minutes=31))

    resumed = recheck_due_providers(registry)

    assert resumed == ["claude"]
    assert registry.get_status("claude").state == ProviderState.AVAILABLE


def test_run_tick_never_lets_two_providers_run_the_same_project_concurrently():
    # Simulate a second worker/provider already processing this project
    # (lock held by "other-worker") - even though a different provider is
    # available, run_tick must not start a second run on the same project.
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    registry.mark_available("gpt")
    lock_manager = ProjectLockManager()
    lock_manager.acquire("Demo", "other-worker")

    calls = []

    def run_fn(project, provider):
        calls.append((project.name, provider))
        return {}

    outcome = run_tick(
        client, registry, run_fn,
        default_providers=["claude", "gpt"],
        lock_manager=lock_manager,
    )

    assert outcome.ran is False
    assert calls == []


def test_run_tick_syncs_full_state_back_to_trello_after_run():
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    def run_fn(project, provider):
        return {
            "checkpoint": {"step": 9},
            "last_output": "did the work",
            "next_step": "next thing",
            "stop_reason": "waiting for review",
            "retry_after": "2026-02-01T00:00:00+00:00",
            "status": "in_progress",
        }

    outcome = run_tick(client, registry, run_fn, default_providers=["claude"])
    assert outcome.ran is True

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)

    assert reloaded.checkpoint == {"step": 9}
    assert reloaded.last_output == "did the work"
    assert reloaded.next_step == "next thing"
    assert reloaded.stop_reason == "waiting for review"
    assert reloaded.retry_after == "2026-02-01T00:00:00+00:00"
    assert reloaded.provider == "claude"
    assert reloaded.status == ProjectStatus.IN_PROGRESS


def test_run_tick_persists_provider_limit_when_trello_result_sync_fails(tmp_path):
    class FailingUpdateClient(InMemoryTrelloClient):
        fail_updates = False

        def update_card(self, card_id, **updates):
            if self.fail_updates:
                raise RuntimeError("temporary Trello write outage")
            return super().update_card(card_id, **updates)

    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = FailingUpdateClient()
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    registry = ProviderRegistry(clock=clock)
    registry.mark_available("claude")
    state_path = tmp_path / "provider-state.json"

    def run_fn(project, provider):
        registry.mark_limited(
            provider,
            retry_after=timedelta(minutes=30),
            checkpoint={"step": 7},
            reason="quota exceeded",
        )
        # The visible READY -> IN_PROGRESS transition succeeded. Simulate
        # the outage only for the result write, which this regression test
        # is specifically intended to cover.
        client.fail_updates = True
        return {
            "status": "in_progress",
            "checkpoint": {"step": 7},
            "retry_after": registry.get_status(provider).retry_after.isoformat(),
        }

    with pytest.raises(RuntimeError, match="temporary Trello write outage"):
        run_tick(
            client,
            registry,
            run_fn,
            default_providers=["claude"],
            provider_state_path=str(state_path),
        )

    reloaded = ProviderRegistry(clock=clock)
    load_provider_state(state_path, reloaded)
    status = reloaded.get_status("claude")
    assert status.state == ProviderState.LIMITED
    assert status.retry_after == clock.now + timedelta(minutes=30)
    assert status.checkpoint == {"step": 7}


def test_run_loop_without_once_runs_multiple_ticks_until_stopped():
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    calls = []

    def run_fn(project, provider):
        calls.append(provider)
        return {}

    outcome = run_loop(
        client, registry, run_fn,
        once=False,
        sleep=lambda s: None,
        default_providers=["claude"],
        max_iterations=3,
    )

    # Unlike once=True (exactly one tick), the plain loop keeps ticking
    # - here for the 3 iterations the test caps it at.
    assert calls == ["claude", "claude", "claude"]
    assert outcome.ran is True


def test_run_loop_only_sleeps_when_a_tick_finds_no_work():
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")
    sleeps = []

    def run_fn(project, provider):
        return {"status": "done"}

    run_loop(
        client,
        registry,
        run_fn,
        once=False,
        sleep=sleeps.append,
        poll_interval_seconds=17,
        default_providers=["claude"],
        max_iterations=3,
        lock_manager=ProjectLockManager(),
    )

    # The first tick did useful work and immediately drains the queue.
    # The second tick is empty and waits; the third ends the bounded loop
    # before another sleep is needed.
    assert sleeps == [17]


def test_run_loop_wakes_at_retry_after_before_normal_poll_interval():
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    registry = ProviderRegistry(clock=clock)
    registry.mark_limited("claude", retry_after=timedelta(seconds=20))
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(timedelta(seconds=seconds))

    calls = []

    def run_fn(project, provider):
        calls.append((project.name, dict(registry.get_status(provider).checkpoint)))
        return {"status": "done"}

    registry.get_status("claude").checkpoint = {"step": 4}
    run_loop(
        client,
        registry,
        run_fn,
        sleep=sleep,
        poll_interval_seconds=300,
        default_providers=["claude"],
        max_iterations=2,
    )

    assert sleeps == [20]
    assert calls == [("Demo", {"step": 4})]


def test_run_loop_never_extends_poll_to_a_later_retry_after():
    client = InMemoryTrelloClient()
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    registry = ProviderRegistry(clock=clock)
    registry.mark_limited("claude", retry_after=timedelta(minutes=30))
    sleeps = []

    run_loop(
        client,
        registry,
        lambda project, provider: {},
        sleep=sleeps.append,
        poll_interval_seconds=60,
        default_providers=["claude"],
        max_iterations=2,
    )

    assert sleeps == [60]


def test_run_loop_accepts_naive_utc_registry_clock_with_aware_retry_deadline():
    client = InMemoryTrelloClient()
    clock = FakeClock(datetime(2026, 1, 1))
    registry = ProviderRegistry(clock=clock)
    registry.mark_limited(
        "claude",
        retry_after=datetime(2026, 1, 1, 0, 0, 20, tzinfo=timezone.utc),
    )
    sleeps = []
    calls = []

    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(timedelta(seconds=seconds))

    def run_fn(project, provider):
        calls.append(provider)
        return {"status": "done"}

    run_loop(
        client,
        registry,
        run_fn,
        sleep=sleep,
        poll_interval_seconds=300,
        default_providers=["claude"],
        max_iterations=2,
    )

    assert sleeps == [20]
    assert calls == ["claude"]
    assert registry.get_status("claude").state == ProviderState.AVAILABLE


def test_run_loop_halts_project_after_repeated_identical_failures_across_ticks():
    """Regression: run_once's OrchestratorGuard only halts a project once
    the *same* failure signature has repeated across calls - but
    run_tick/run_loop never threaded a guard through at all, so
    run_once's default (``guard or OrchestratorGuard()``) built a brand
    new, empty guard on every single tick. The repeat count could never
    accumulate across ticks, so a project stuck failing with the exact
    same error (e.g. a permission-denial loop) was retried forever
    instead of ever being halted - defeating the whole protection this
    guard exists for in real, multi-tick, unattended operation."""
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    calls = []

    def run_fn(project, provider):
        calls.append(provider)
        raise RuntimeError("permission denied: rm -rf /")

    run_loop(
        client, registry, run_fn,
        once=False,
        sleep=lambda s: None,
        default_providers=["claude"],
        max_iterations=10,
        lock_manager=ProjectLockManager(),
    )

    # OrchestratorGuard's default max_repeats=2 halts on the 3rd
    # identical failure - run_fn must never be called again after that,
    # across however many further ticks the loop runs.
    assert len(calls) == 3

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status == ProjectStatus.BLOCKED
    assert "permission denied" in reloaded.blocked_by


def test_run_tick_logs_selected_project_provider_dispatch_result_and_sync(caplog):
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    def run_fn(project, provider):
        return {"status": "in_progress", "last_output": "worked"}

    with caplog.at_level(logging.INFO, logger="ai_project_manager"):
        run_tick(client, registry, run_fn, default_providers=["claude"])

    messages = "\n".join(caplog.messages)
    assert "Demo" in messages
    assert "claude" in messages
    assert "autonomous" in messages.lower()
    assert "synced" in messages.lower()


def test_run_tick_logs_wait_when_provider_is_limited(caplog):
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = make_client_with_project(project)
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    registry = ProviderRegistry(clock=clock)
    registry.mark_limited("claude", retry_after=timedelta(minutes=30))

    def run_fn(project, provider):
        return {}

    with caplog.at_level(logging.INFO, logger="ai_project_manager"):
        run_tick(client, registry, run_fn, default_providers=["claude"])

    messages = "\n".join(caplog.messages)
    assert "claude" in messages
    assert "retry_after" in messages.lower()


def test_bootstrap_project_key_never_infers_identity_from_card_content():
    client = InMemoryTrelloClient(
        list_names=(
            "INBOX / Nápady",
            "Připraveno",
            "Pracuje se",
            "Čeká na AI",
            "Testování",
            "Hotovo",
        )
    )
    _, name_to_id = build_list_maps(client)
    card = client.create_card(
        name_to_id["Připraveno"],
        "P5 — Izolace testovacích Slack notifikací",
        desc=(
            "CÍL: Opravit AI Project Manager / testovací ověřovací skripty.\n\n"
            "KONTEXT: během ověřování se objevily falešné zprávy pro Station Agent.\n"
            "BEZPEČNOST: žádné změny Station Agent funkcí."
        ),
        labels=["P5"],
    )

    project_paths = {
        "AI Project Manager": r"D:\orchestrator\ai-project-manager",
        "AI Orchestrator": r"D:\orchestrator\ai-orchestrator",
        "Station Agent": r"D:\orchestrator\station-agent",
        "P5 — Stabilní mapování Trello projektu na lokální cestu": r"D:\orchestrator\ai-project-manager",
    }

    projects = load_projects_and_inbox(
        client,
        inbox_list_name="INBOX / Nápady",
        project_paths=project_paths,
    )

    migrated = next(p for p in projects if p.trello_card_id == card["id"])
    assert migrated.project_key is None

    reloaded = client.get_card(card["id"])
    assert {label["name"] for label in reloaded["labels"]} == {"P5"}


def test_inbox_intake_skips_ai_when_prepared_work_has_phase_precedence():
    client = InMemoryTrelloClient(
        list_names=(
            "INBOX / Nápady",
            "Připraveno",
            "Pracuje se",
            "Čeká na AI",
            "Testování",
            "Hotovo",
        )
    )
    _, name_to_id = build_list_maps(client)
    ready = client.create_card(
        name_to_id["Připraveno"],
        "P5.10 — Station Agent — oprava spuštění",
        desc="Opravit Station Agent.",
        labels=["P5.10", "Station Agent"],
    )
    inbox = client.create_card(
        name_to_id["INBOX / Nápady"],
        "Nový projekt",
        desc="Vytvořit nový projekt.",
    )
    planner_calls = []

    projects = load_projects_and_inbox(
        client,
        inbox_list_name="INBOX / Nápady",
        process_inbox_enabled=True,
        planner=lambda *_: planner_calls.append(True),
    )

    assert planner_calls == []
    assert client.get_card(inbox["id"])["list_id"] == name_to_id["INBOX / Nápady"]
    assert any(project.trello_card_id == ready["id"] for project in projects)


def test_bootstrap_project_key_does_not_guess_on_zero_or_ambiguous_match():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)

    zero = client.create_card(
        name_to_id["Ready"],
        "P5 — Obecný úkol",
        desc="CÍL: Udělat obecnou údržbu bez názvu projektu.",
        labels=["P5"],
    )
    ambiguous = client.create_card(
        name_to_id["Ready"],
        "P5 — Integrační úkol",
        desc="CÍL: Propojit AI Project Manager a Station Agent.",
        labels=["P5"],
    )

    project_paths = {
        "AI Project Manager": r"D:\orchestrator\ai-project-manager",
        "AI Orchestrator": r"D:\orchestrator\ai-orchestrator",
        "Station Agent": r"D:\orchestrator\station-agent",
    }

    projects = load_projects_and_inbox(client, project_paths=project_paths)
    by_id = {p.trello_card_id: p for p in projects}

    assert by_id[zero["id"]].project_key is None
    assert by_id[ambiguous["id"]].project_key is None
    assert {label["name"] for label in client.get_card(zero["id"])["labels"]} == {"P5"}
    assert {label["name"] for label in client.get_card(ambiguous["id"])["labels"]} == {"P5"}


def test_bootstrap_project_key_never_infers_identity_from_exact_title_path():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)

    card = client.create_card(
        name_to_id["Ready"],
        "P5 — Oprava DoD/checkpoint indexování",
        desc="CÍL: Opravit checkpoint indexování.",
        labels=["P5"],
    )

    project_paths = {
        "AI Project Manager": r"D:\orchestrator\ai-project-manager",
        "AI Orchestrator": r"D:\orchestrator\ai-orchestrator",
        "Station Agent": r"D:\orchestrator\station-agent",
        "P5 — Oprava DoD/checkpoint indexování": r"D:\orchestrator\ai-orchestrator",
    }

    projects = load_projects_and_inbox(client, project_paths=project_paths)
    migrated = next(p for p in projects if p.trello_card_id == card["id"])

    assert migrated.project_key is None
    assert {label["name"] for label in client.get_card(card["id"])["labels"]} == {"P5"}


def test_bootstrap_project_key_migrates_real_production_card_via_card_id_override():
    """Real production regression (2026-08-26): the live board's card
    "P5 - Izolace testovacich Slack notifikaci" carries only a P5 priority
    label - the board itself only has P1/P4/P5 labels at all, no project
    identity label ever existed - and its description names no project
    ("KONTEXT: overit ze testovaci Slack zpravy nechodi do produkcniho
    kanalu."), so neither the title nor the content-phrase heuristic can
    resolve it. AI_PM_CARD_PROJECT_KEYS is the safe, deterministic,
    non-guessing migration path for exactly this case: keyed by the card's
    immutable ID, never its free-form text."""
    client = InMemoryTrelloClient(
        list_names=("INBOX / Nápady", "Připraveno", "Pracuje se", "Čeká na AI", "Testování", "Hotovo"),
    )
    _, name_to_id = build_list_maps(client)
    card = client.create_card(
        name_to_id["Připraveno"],
        "P5 — Izolace testovacích Slack notifikací",
        desc="KONTEXT: ověřit že testovací Slack zprávy nechodí do produkčního kanálu.",
        labels=["P5"],
    )

    project_paths = {
        "AI Project Manager": r"D:\orchestrator\ai-project-manager",
        "AI Orchestrator": r"D:\orchestrator\ai-orchestrator",
        "Station Agent": r"D:\orchestrator\station-agent",
    }

    # Without the migration input, the card cannot be resolved at all -
    # confirms the reported root cause and that nothing guesses a project.
    unmigrated = load_projects_and_inbox(client, inbox_list_name="INBOX / Nápady", project_paths=project_paths)
    assert next(p for p in unmigrated if p.trello_card_id == card["id"]).project_key is None

    projects = load_projects_and_inbox(
        client,
        inbox_list_name="INBOX / Nápady",
        project_paths=project_paths,
        card_project_keys={card["id"]: "AI Project Manager"},
    )

    migrated = next(p for p in projects if p.trello_card_id == card["id"])
    assert migrated.project_key == "AI Project Manager"
    assert {label["name"] for label in client.get_card(card["id"])["labels"]} == {
        "P5",
        "AI Project Manager",
    }

    # The label survives a later status/priority sync unchanged.
    migrated.priority = 4
    sync_project_to_trello(client, migrated)
    assert {label["name"] for label in client.get_card(card["id"])["labels"]} == {
        "P4",
        "AI Project Manager",
    }


def test_bootstrap_project_key_migrates_real_production_card_via_exact_title_override():
    """Mirrors the actual production launcher (scripts/run-ai-project-manager.ps1),
    which cannot know the card's internal Trello ID ahead of time and so
    seeds AI_PM_CARD_PROJECT_KEYS keyed by the card's exact current title
    instead. Still an exact-equality lookup, never a phrase/substring
    scan, so it is exactly as safe/non-guessing as the ID-keyed form."""
    client = InMemoryTrelloClient(
        list_names=("INBOX / Nápady", "Připraveno", "Pracuje se", "Čeká na AI", "Testování", "Hotovo"),
    )
    _, name_to_id = build_list_maps(client)
    card_title = "P5 — Izolace testovacích Slack notifikací"
    card = client.create_card(
        name_to_id["Připraveno"],
        card_title,
        desc="KONTEXT: ověřit že testovací Slack zprávy nechodí do produkčního kanálu.",
        labels=["P5"],
    )

    project_paths = {
        "AI Project Manager": r"D:\orchestrator\ai-project-manager",
        "AI Orchestrator": r"D:\orchestrator\ai-orchestrator",
        "Station Agent": r"D:\orchestrator\station-agent",
    }

    projects = load_projects_and_inbox(
        client,
        inbox_list_name="INBOX / Nápady",
        project_paths=project_paths,
        card_project_keys={card_title: "AI Project Manager"},
    )

    migrated = next(p for p in projects if p.trello_card_id == card["id"])
    assert migrated.project_key == "AI Project Manager"
    assert {label["name"] for label in client.get_card(card["id"])["labels"]} == {
        "P5",
        "AI Project Manager",
    }


def test_bootstrap_project_key_card_id_override_takes_precedence_over_title_override():
    """When both an ID and a (possibly stale) title entry exist for the
    same card, the immutable ID is authoritative."""
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    card_title = "P5 — Obecný úkol"
    card = client.create_card(
        name_to_id["Ready"],
        card_title,
        desc="CÍL: Udělat obecnou údržbu bez názvu projektu.",
        labels=["P5"],
    )

    project_paths = {
        "AI Project Manager": r"D:\orchestrator\ai-project-manager",
        "AI Orchestrator": r"D:\orchestrator\ai-orchestrator",
        "Station Agent": r"D:\orchestrator\station-agent",
    }

    projects = load_projects_and_inbox(
        client,
        project_paths=project_paths,
        card_project_keys={
            card["id"]: "AI Orchestrator",
            card_title: "Station Agent",
        },
    )

    migrated = next(p for p in projects if p.trello_card_id == card["id"])
    assert migrated.project_key == "AI Orchestrator"


def test_bootstrap_project_key_title_override_refuses_duplicate_card_titles():
    """An exact-title fallback must never stamp duplicate cards."""
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    title = "P5 — Obecný úkol"
    cards = [
        client.create_card(
            name_to_id["Ready"],
            title,
            desc=f"CÍL: samostatný úkol {index}",
            labels=["P5"],
        )
        for index in range(2)
    ]
    project_paths = {
        "AI Project Manager": r"D:\orchestrator\ai-project-manager",
        "Station Agent": r"D:\orchestrator\station-agent",
    }

    projects = load_projects_and_inbox(
        client,
        project_paths=project_paths,
        card_project_keys={title: "AI Project Manager"},
    )

    by_id = {project.trello_card_id: project for project in projects}
    assert all(by_id[card["id"]].project_key is None for card in cards)
    assert all(
        {label["name"] for label in client.get_card(card["id"])["labels"]} == {"P5"}
        for card in cards
    )


def test_bootstrap_project_key_card_id_override_ignores_unknown_project_identity():
    """A typo'd or stale AI_PM_CARD_PROJECT_KEYS entry naming an identity
    that is not one of the configured stable projects must never fabricate
    a bogus label or silently point the card at an arbitrary repo."""
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    card = client.create_card(
        name_to_id["Ready"],
        "P5 — Obecný úkol",
        desc="CÍL: Udělat obecnou údržbu bez názvu projektu.",
        labels=["P5"],
    )

    project_paths = {
        "AI Project Manager": r"D:\orchestrator\ai-project-manager",
        "AI Orchestrator": r"D:\orchestrator\ai-orchestrator",
        "Station Agent": r"D:\orchestrator\station-agent",
    }

    projects = load_projects_and_inbox(
        client,
        project_paths=project_paths,
        card_project_keys={card["id"]: "Some Unrelated Project"},
    )

    migrated = next(p for p in projects if p.trello_card_id == card["id"])
    assert migrated.project_key is None
    assert {label["name"] for label in client.get_card(card["id"])["labels"]} == {"P5"}


@pytest.mark.parametrize("priority", [0, 1, 2, 3, 4, 5])
@pytest.mark.parametrize(
    "project_key,repo_path",
    [
        ("AI Project Manager", r"D:\orchestrator\ai-project-manager"),
        ("AI Orchestrator", r"D:\orchestrator\ai-orchestrator"),
        ("Station Agent", r"D:\orchestrator\station-agent"),
    ],
)
def test_bootstrap_and_resolve_matrix_three_repos_all_priorities_production_board(
    priority, project_key, repo_path
):
    """Full regression matrix for the DoD requirement: every one of the 3
    real repositories at every P0-P5 priority must migrate to its own
    correct project identity and never cross-map to a different repo, on
    a board that (like the real production board) starts out with only
    bare priority labels and no project identity labels at all."""
    from ai_project_manager.orchestrator_runner import resolve_project_path

    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    card = client.create_card(
        name_to_id["Ready"],
        f"P{priority} — generický pracovní úkol bez zmínky o projektu",
        desc="CÍL: proveď úkol popsaný v checklistu níže.",
        labels=[f"P{priority}"],
    )

    all_project_paths = {
        "AI Project Manager": r"D:\orchestrator\ai-project-manager",
        "AI Orchestrator": r"D:\orchestrator\ai-orchestrator",
        "Station Agent": r"D:\orchestrator\station-agent",
    }

    projects = load_projects_and_inbox(
        client,
        project_paths=all_project_paths,
        card_project_keys={card["id"]: project_key},
    )

    migrated = next(p for p in projects if p.trello_card_id == card["id"])
    assert migrated.project_key == project_key
    assert migrated.priority == priority
    assert resolve_project_path(migrated, project_paths=all_project_paths) == repo_path


def test_production_board_migration_maps_each_card_to_its_own_repo_without_cross_mapping():
    """Single-board regression combining every DoD requirement at once:
    a board that (like the real production board on 2026-08-26) starts
    out with only bare P0-P5 priority labels and no project identity
    label anywhere, carrying a realistic mix of cards for all 3 real
    repositories plus the exact real "P5 - Izolace testovacich Slack
    notifikaci" card and the pre-existing zero/ambiguous-content cards -
    all migrated in a single ``load_projects_and_inbox`` call (the same
    call a real ``--once`` tick makes). Every card must end up pointing
    at its own repo, never another card's, and cards with no safe
    migration signal must stay unmapped rather than guess."""
    from ai_project_manager.orchestrator_runner import resolve_project_path

    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)

    all_project_paths = {
        "AI Project Manager": r"D:\orchestrator\ai-project-manager",
        "AI Orchestrator": r"D:\orchestrator\ai-orchestrator",
        "Station Agent": r"D:\orchestrator\station-agent",
    }

    real_slack_card = client.create_card(
        name_to_id["Ready"],
        "P5 — Izolace testovacích Slack notifikací",
        desc="KONTEXT: ověřit že testovací Slack zprávy nechodí do produkčního kanálu.",
        labels=["P5"],
    )
    orchestrator_card = client.create_card(
        name_to_id["Ready"],
        "P4 — Oprava retry backoff v provider registry",
        desc="CÍL: opravit retry backoff.",
        labels=["P4"],
    )
    station_card = client.create_card(
        name_to_id["Ready"],
        "P1 — DX Cluster scoring propagace",
        desc="CÍL: opravit scoring propagaci.",
        labels=["P1"],
    )
    zero_signal_card = client.create_card(
        name_to_id["Ready"],
        "P5 — Obecný úkol",
        desc="CÍL: Udělat obecnou údržbu bez názvu projektu.",
        labels=["P5"],
    )
    ambiguous_card = client.create_card(
        name_to_id["Ready"],
        "P5 — Integrační úkol",
        desc="CÍL: Propojit AI Project Manager a Station Agent.",
        labels=["P5"],
    )

    # The board itself, before migration, has only priority labels - the
    # exact reported production state.
    for card in (real_slack_card, orchestrator_card, station_card, zero_signal_card, ambiguous_card):
        assert {label["name"] for label in client.get_card(card["id"])["labels"]} == {
            card["labels"][0]["name"]
        }

    card_project_keys = {
        real_slack_card["id"]: "AI Project Manager",
        orchestrator_card["id"]: "AI Orchestrator",
        station_card["id"]: "Station Agent",
        # A stale/typo'd entry naming an identity that does not exist in
        # AI_PM_PROJECT_PATHS must never fabricate a label or affect any
        # other card.
        zero_signal_card["id"]: "Nonexistent Project",
    }

    projects = load_projects_and_inbox(
        client,
        project_paths=all_project_paths,
        card_project_keys=card_project_keys,
    )
    by_id = {p.trello_card_id: p for p in projects}

    assert by_id[real_slack_card["id"]].project_key == "AI Project Manager"
    assert by_id[orchestrator_card["id"]].project_key == "AI Orchestrator"
    assert by_id[station_card["id"]].project_key == "Station Agent"
    assert by_id[zero_signal_card["id"]].project_key is None
    assert by_id[ambiguous_card["id"]].project_key is None

    assert (
        resolve_project_path(by_id[real_slack_card["id"]], project_paths=all_project_paths)
        == r"D:\orchestrator\ai-project-manager"
    )
    assert (
        resolve_project_path(by_id[orchestrator_card["id"]], project_paths=all_project_paths)
        == r"D:\orchestrator\ai-orchestrator"
    )
    assert (
        resolve_project_path(by_id[station_card["id"]], project_paths=all_project_paths)
        == r"D:\orchestrator\station-agent"
    )

    # Every migrated card's label set is exactly {its priority, its own
    # identity} - never another card's identity, never more than one.
    assert {label["name"] for label in client.get_card(real_slack_card["id"])["labels"]} == {
        "P5",
        "AI Project Manager",
    }
    assert {label["name"] for label in client.get_card(orchestrator_card["id"])["labels"]} == {
        "P4",
        "AI Orchestrator",
    }
    assert {label["name"] for label in client.get_card(station_card["id"])["labels"]} == {
        "P1",
        "Station Agent",
    }
    # Unmapped cards are left exactly as they were - no fabricated label.
    assert {label["name"] for label in client.get_card(zero_signal_card["id"])["labels"]} == {"P5"}
    assert {label["name"] for label in client.get_card(ambiguous_card["id"])["labels"]} == {"P5"}


# ---- unattended blocked-task recovery pass (see recovery.py) -----------


def test_run_tick_auto_recovers_transiently_blocked_project_and_dispatches_it_same_tick():
    """The scheduler itself never picks a BLOCKED project (scheduler.is_schedulable),
    so without a recovery pass this card would stay abandoned forever.
    A transient provider/protocol error is safely auto-recoverable: the
    recovery pass must unblock it, preserve its priority/checkpoint, and
    the very same tick must then dispatch it to run_fn."""
    project = ProjectRecord(
        name="Demo",
        priority=4,
        status=ProjectStatus.BLOCKED,
        orchestrator_ready_task="Finish the widget",
        checkpoint={"step": 7},
        blocked_by="connection reset while calling the provider",
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    calls = []

    def run_fn(project, provider):
        calls.append((project.name, project.checkpoint))
        return {"status": "in_progress", "last_output": "resumed"}

    outcome = run_tick(client, registry, run_fn, default_providers=["claude"])

    assert outcome.ran is True
    assert calls == [("Demo", {"step": 7})]

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.priority == 4
    assert reloaded.blocked_by is None
    assert reloaded.status == ProjectStatus.IN_PROGRESS


def test_run_tick_leaves_human_required_block_in_place_and_never_dispatches():
    project = ProjectRecord(
        name="Demo",
        priority=5,
        status=ProjectStatus.BLOCKED,
        orchestrator_ready_task="Finish the widget",
        blocked_by="missing API key credentials for the deploy target",
    )
    client = make_client_with_project(project)
    registry = ProviderRegistry()
    registry.mark_available("claude")

    calls = []

    def run_fn(project, provider):
        calls.append(project.name)
        return {}

    outcome = run_tick(client, registry, run_fn, default_providers=["claude"])

    assert outcome.ran is False
    assert calls == []

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status == ProjectStatus.BLOCKED
    assert "credential" in reloaded.blocked_by.lower() or "api" in reloaded.blocked_by.lower()


def test_human_required_notification_is_deduplicated_and_resume_notifies_once(monkeypatch):
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    registry = ProviderRegistry(clock=clock)
    project = ProjectRecord(
        name="Deploy widget",
        status=ProjectStatus.BLOCKED,
        orchestrator_ready_task="Deploy the widget",
        blocked_by="missing API key credentials for production",
    )
    client = make_client_with_project(project)
    project.trello_card_url = "https://trello.example/c/card-1"
    messages = []
    monkeypatch.setattr("ai_project_manager.daemon.notify", messages.append)

    _run_recovery_pass(client, [project], registry, DEFAULT_MAX_ATTEMPTS, default_backoff)
    # Make the identical state due for review again; it must be persisted but
    # must not produce a second Slack notification.
    clock.advance(timedelta(days=2))
    project.review_at = clock.now.isoformat()
    _run_recovery_pass(client, [project], registry, DEFAULT_MAX_ATTEMPTS, default_backoff)

    assert len(messages) == 1
    assert "Deploy widget" in messages[0]
    assert project.trello_card_url in messages[0]
    assert "Důvod:" in messages[0]
    assert "Krok:" in messages[0]
    visible = client.get_card(project.trello_card_id)["desc"].split("<!-- PM-DATA", 1)[0]
    assert "VYŽADUJE LIDSKÝ ZÁSAH" in visible
    assert project.human_notified_reason in visible
    assert project.human_action_step in visible

    project.status = ProjectStatus.READY
    project.blocked_by = None
    project.review_at = None
    _run_recovery_pass(client, [project], registry, DEFAULT_MAX_ATTEMPTS, default_backoff)
    _run_recovery_pass(client, [project], registry, DEFAULT_MAX_ATTEMPTS, default_backoff)

    assert len(messages) == 2
    assert "Pokračuji" in messages[1]
    assert project.human_notified_reason is None
    assert project.human_action_step is None


def test_human_required_card_stays_short_and_deduplicated_across_reloaded_recovery_ticks(monkeypatch):
    """Regression for the live-verified bug: a card whose blocked reason
    matches no known auto-recoverable pattern kept getting its blocked_by
    reason wrapped in another layer of "blocked reason (...) does not
    match a known auto-recoverable pattern; needs human triage" on every
    recovery pass, and Slack re-sent the "Vyžaduje zásah člověka" message
    every time because the (ever-growing) reason text never matched the
    previous one.

    This drives the exact daemon.run_tick-shaped cycle - recover, sync to
    Trello, reload a fresh ProjectRecord from the card (as the next real
    tick would, via trello_sync.fetch_all_projects) - repeated across
    three recovery ticks, and asserts the Trello reason and the Slack
    message both stay byte-for-byte stable throughout.
    """
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    registry = ProviderRegistry(clock=clock)
    original_reason = "something bespoke that matches no known pattern at all"
    project = ProjectRecord(
        name="Weird block",
        status=ProjectStatus.BLOCKED,
        orchestrator_ready_task="Finish the widget",
        blocked_by=original_reason,
    )
    client = make_client_with_project(project)
    messages = []
    monkeypatch.setattr("ai_project_manager.daemon.notify", messages.append)

    id_to_name, _ = build_list_maps(client)

    for tick in range(3):
        # Reload from the Trello card, exactly like the real daemon loop
        # does every tick (see trello_sync.fetch_all_projects) - never
        # reuse the in-process object, so any accidental self-referential
        # wrapping of a previous pass's output would actually surface.
        reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
        _run_recovery_pass(client, [reloaded], registry, DEFAULT_MAX_ATTEMPTS, default_backoff)

        assert reloaded.blocked_by == original_reason, f"tick {tick}: blocked_by was rewrapped"
        assert "blocked reason (" not in (reloaded.blocked_by or "")

        clock.advance(timedelta(hours=24))

    assert len(messages) == 1, f"expected exactly one Slack notification, got {len(messages)}: {messages}"
    assert original_reason in messages[0]
    assert "blocked reason (" not in messages[0]

    final = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert final.blocked_by == original_reason
    assert final.status == ProjectStatus.BLOCKED


def test_recovery_persists_each_duplicate_title_card_by_immutable_id():
    """Trello card titles are not identities: recovery must not collapse
    two same-named cards and write both outcomes to whichever one appeared
    last in the board response."""
    client = InMemoryTrelloClient()
    projects = [
        ProjectRecord(
            name="Duplicate",
            status=ProjectStatus.BLOCKED,
            orchestrator_ready_task="First task",
            blocked_by="missing API key credentials for first target",
        ),
        ProjectRecord(
            name="Duplicate",
            status=ProjectStatus.BLOCKED,
            orchestrator_ready_task="Second task",
            blocked_by="access denied for second target",
        ),
    ]
    card_ids = []
    for project in projects:
        created = sync_project_to_trello(client, project)
        project.trello_card_id = created["id"]
        card_ids.append(created["id"])

    registry = ProviderRegistry()
    registry.mark_available("claude")
    outcome = run_tick(
        client,
        registry,
        lambda _project, _provider: pytest.fail("human-required card was dispatched"),
        default_providers=["claude"],
    )

    assert outcome.ran is False
    id_to_name, _ = build_list_maps(client)
    reloaded = [project_from_card(client.get_card(card_id), id_to_name) for card_id in card_ids]
    assert all(project.status == ProjectStatus.BLOCKED for project in reloaded)
    # blocked_by keeps the exact short original reason - never rewrapped.
    assert reloaded[0].blocked_by == "missing API key credentials for first target"
    assert reloaded[1].blocked_by == "access denied for second target"


def test_run_tick_does_not_rescan_a_blocked_project_before_its_review_at():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    registry = ProviderRegistry(clock=clock)
    registry.mark_available("claude")

    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.BLOCKED,
        orchestrator_ready_task="Finish the widget",
        blocked_by="connection reset while calling the provider",
        review_at=(clock.now + timedelta(hours=1)).isoformat(),
    )
    client = make_client_with_project(project)

    calls = []

    def run_fn(project, provider):
        calls.append(project.name)
        return {}

    outcome = run_tick(client, registry, run_fn, default_providers=["claude"])

    assert outcome.ran is False
    assert calls == []

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    # Untouched: still blocked with the exact same reason, not rewritten.
    assert reloaded.blocked_by == "connection reset while calling the provider"


def test_run_tick_recovery_never_retries_the_same_block_forever():
    """Regression for the loop guard: a project that keeps re-blocking
    with the exact same transient signature after every auto-recovery
    requeue must eventually be forced to a permanent human-required state
    instead of being requeued forever."""
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    registry = ProviderRegistry(clock=clock)
    registry.mark_available("claude")

    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.BLOCKED,
        orchestrator_ready_task="Finish the widget",
        blocked_by="connection reset while calling the provider",
    )
    client = make_client_with_project(project)

    dispatch_count = 0

    def run_fn(project, provider):
        nonlocal dispatch_count
        dispatch_count += 1
        # The dispatched run immediately hits the exact same transient
        # failure again - the worst case for a naive always-retry
        # implementation.
        return {"status": "blocked", "stop_reason": "connection reset while calling the provider"}

    for _ in range(DEFAULT_MAX_ATTEMPTS + 3):
        run_tick(client, registry, run_fn, default_providers=["claude"])
        clock.advance(timedelta(hours=24))

    id_to_name, _ = build_list_maps(client)
    final = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert final.status == ProjectStatus.BLOCKED
    # The diagnosis ("exhausted") lives in the human-facing notice, never
    # rewritten back into blocked_by - which the dispatched run here never
    # set in the first place (its "blocked" result carries stop_reason, not
    # blocked_by; see runner._apply_run_result).
    assert "exhausted" in (final.human_notified_reason or "")
    assert final.blocked_by is None


def test_run_tick_notifies_visibly_when_a_card_contract_is_unsafe_to_migrate(monkeypatch):
    """DoD: an invalid/newer Trello Card Contract must never be silently
    overwritten - it must also raise a visible notice (Slack), not just a
    log line, so a human actually notices the card needs attention."""
    client = InMemoryTrelloClient()
    ready = client.get_list_id_by_name("Ready")
    bad_card = client.create_card(
        ready, "Wrong authority",
        desc='<!-- PM-DATA\n{"schema_version": 1, "checkpoint": {}, "dod": [], '
             '"open_feedback": [], "governance": {"source_of_truth": "local-files"}}\n-->',
    )
    before = client.get_card(bad_card["id"])
    registry = ProviderRegistry()

    messages = []
    monkeypatch.setattr("ai_project_manager.daemon.notify", messages.append)

    run_tick(client, registry, lambda project, provider: {}, default_providers=["claude"])

    assert any("Trello Card Contract" in message for message in messages)
    assert any(bad_card["id"] in message or "Wrong authority" in message for message in messages)
    assert client.get_card(bad_card["id"]) == before
