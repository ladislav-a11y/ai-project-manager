import logging
from datetime import datetime, timedelta, timezone

from ai_project_manager.daemon import recheck_due_providers, run_loop, run_tick
from ai_project_manager.lock import ProjectLockManager
from ai_project_manager.models import ProjectRecord, ProjectStatus
from ai_project_manager.providers import ProviderRegistry, ProviderState
from ai_project_manager.trello_client import InMemoryTrelloClient
from ai_project_manager.trello_sync import build_list_maps, project_from_card, sync_project_to_trello


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


def test_run_tick_loads_real_projects_and_processes_inbox_then_runs():
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

    outcome = run_tick(client, registry, run_fn, default_providers=["claude"])

    assert outcome.ran is True
    assert calls == ["Dashboard"]

    # The inbox card was folded into the project and marked processed so
    # a later tick won't reapply the same feedback.
    inbox_cards = client.list_cards(name_to_id["Inbox"])
    assert "PM-INBOX-PROCESSED" in inbox_cards[0]["desc"]


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


def test_run_loop_auto_resumes_project_after_retry_after_without_manual_intervention():
    # IN_PROGRESS, not PAUSED: a provider-limit stop must leave the
    # project schedulable (see orchestrator_runner._mark_limited_result)
    # so it resumes on its own once the provider is available again.
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.IN_PROGRESS,
        checkpoint={"step": 5},
        stop_reason="provider session limit hit",
    )
    client = make_client_with_project(project)
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
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
    # free (no probe network/AI call - _default_probe just returns True)
    # and the paused project resumes automatically from its checkpoint.
    clock.advance(timedelta(minutes=25))
    outcome_2 = run_tick(client, registry, run_fn, default_providers=["claude"])

    assert outcome_2.ran is True
    assert calls == [("Demo", {"step": 5})]
    assert registry.get_status("claude").state == ProviderState.AVAILABLE


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
