from datetime import timedelta

from ai_project_manager.guard import OrchestratorGuard
from ai_project_manager.lock import ProjectLockManager
from ai_project_manager.models import ProjectRecord, ProjectStatus
from ai_project_manager.providers import ProviderRegistry
from ai_project_manager.runner import run_once
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
