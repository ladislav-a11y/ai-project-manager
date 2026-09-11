"""V2 regression checks for the single active slot and dependency context."""

import pytest

from ai_project_manager.daemon import run_tick
from ai_project_manager.models import DoDItem, ProjectRecord, ProjectStatus
from ai_project_manager.providers import ProviderRegistry
from ai_project_manager.trello_client import InMemoryTrelloClient
from ai_project_manager.trello_sync import build_list_maps, project_from_card, sync_project_to_trello


@pytest.fixture(autouse=True)
def _isolate_provider_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _implementation_plus_audit_dod():
    return [
        DoDItem(text="implementation", phase="implementation", checked=True),
        DoDItem(text="independent audit", phase="audit", checked=False),
    ]


def test_active_card_keeps_completed_sibling_dependency_in_tick():
    client = InMemoryTrelloClient()
    for name, status, index, dependencies in [
        ("Completed design", ProjectStatus.DONE, 0, []),
        ("Active implementation", ProjectStatus.IN_PROGRESS, 1, [0]),
    ]:
        project = ProjectRecord(
            name=name, status=status, main_task="Implement the feature",
            dod=[DoDItem(text="implementation", phase="implementation", checked=status == ProjectStatus.DONE)],
        )
        project.extra_data["inbox_preparation"] = {
            "source_card_id": "source", "subtask_index": index,
            "depends_on_subtask_indices": dependencies,
        }
        sync_project_to_trello(client, project)
    calls = []

    def run_fn(project, provider):
        calls.append((project.name, provider))
        raise RuntimeError("test stops after confirmed dispatch")

    outcome = run_tick(client, ProviderRegistry(), run_fn)
    assert outcome.ran is True
    assert calls == [("Active implementation", "provider-broker")]


def test_ready_card_cannot_bypass_occupied_pracuje_se_slot():
    """V2 keeps one implementation card in Pracuje se at a time."""
    active = ProjectRecord(
        name="Active implementation",
        priority=5.2,
        status=ProjectStatus.IN_PROGRESS,
        main_task="Finish the active feature",
        dod=_implementation_plus_audit_dod(),
    )
    ready = ProjectRecord(
        name="Next implementation",
        priority=5.1,
        status=ProjectStatus.READY,
        main_task="Start the next feature",
        dod=[DoDItem(text="implementation", phase="implementation", checked=False)],
    )
    client = InMemoryTrelloClient()
    active_card = sync_project_to_trello(client, active)
    active.trello_card_id = active_card["id"]
    ready_card = sync_project_to_trello(client, ready)
    ready.trello_card_id = ready_card["id"]
    registry = ProviderRegistry()
    registry.mark_available("claude")
    calls = []

    def run_fn(project, _provider):
        calls.append(project.name)
        raise AssertionError("READY card bypassed the occupied implementation slot")

    def finalize_fn(_project):
        return {"status": "blocked", "stop_reason": "controller finalization unavailable"}

    outcome = run_tick(
        client,
        registry,
        run_fn,
        finalize_fn=finalize_fn,
        default_providers=["claude"],
    )

    assert outcome.ran is False
    assert calls == []
    id_to_name, _ = build_list_maps(client)
    active_reloaded = project_from_card(client.get_card(active.trello_card_id), id_to_name)
    ready_reloaded = project_from_card(client.get_card(ready.trello_card_id), id_to_name)
    assert active_reloaded.status == ProjectStatus.IN_PROGRESS
    assert ready_reloaded.status == ProjectStatus.READY
