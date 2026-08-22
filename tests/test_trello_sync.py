from ai_project_manager.models import GitHubRef, GoogleDriveRef, ProjectRecord, ProjectStatus
from ai_project_manager.trello_client import InMemoryTrelloClient
from ai_project_manager.trello_sync import (
    build_list_maps,
    fetch_all_projects,
    priority_from_labels,
    project_from_card,
    sync_project_to_trello,
)


def test_priority_from_labels_reads_p_label():
    assert priority_from_labels([{"name": "P3"}]) == 3
    assert priority_from_labels([{"name": "bug"}, {"name": "P5"}]) == 5
    assert priority_from_labels([{"name": "bug"}]) == 0
    assert priority_from_labels([]) == 0


def test_sync_project_to_trello_is_source_of_truth_round_trip():
    client = InMemoryTrelloClient()
    project = ProjectRecord(
        name="Orchestrator UI",
        priority=4,
        status=ProjectStatus.IN_PROGRESS,
        main_task="Build the dashboard",
        open_feedback=["spinner never stops"],
        next_step="Wire up the websocket",
        orchestrator_ready_task="Implement websocket client in dashboard.ts",
        last_output="Static layout done",
        checkpoint={"commit": "abc123", "step": "layout"},
        blocked_by=None,
        github_repo=GitHubRef(repo_url="https://github.com/acme/orchestrator-ui", default_branch="main"),
        google_drive_ref=GoogleDriveRef(url="https://drive.google.com/folder/spec", label="spec"),
    )

    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(created["id"]), id_to_name)

    assert reloaded.name == "Orchestrator UI"
    assert reloaded.priority == 4
    assert reloaded.status == ProjectStatus.IN_PROGRESS
    assert reloaded.main_task == "Build the dashboard"
    assert reloaded.open_feedback == ["spinner never stops"]
    assert reloaded.next_step == "Wire up the websocket"
    assert reloaded.orchestrator_ready_task == "Implement websocket client in dashboard.ts"
    assert reloaded.last_output == "Static layout done"
    assert reloaded.checkpoint == {"commit": "abc123", "step": "layout"}
    assert reloaded.github_repo.repo_url == "https://github.com/acme/orchestrator-ui"
    assert reloaded.github_repo.default_branch == "main"
    assert reloaded.google_drive_ref.url == "https://drive.google.com/folder/spec"
    assert reloaded.google_drive_ref.label == "spec"


def test_sync_updates_existing_card_in_place_without_duplication():
    client = InMemoryTrelloClient()
    project = ProjectRecord(name="Demo", priority=1, status=ProjectStatus.NEW, main_task="First")
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    project.status = ProjectStatus.DONE
    project.last_output = "Shipped"
    project.priority = 2
    sync_project_to_trello(client, project)

    all_cards = client.list_all_cards()
    assert len(all_cards) == 1

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(all_cards[0], id_to_name)
    assert reloaded.status == ProjectStatus.DONE
    assert reloaded.last_output == "Shipped"
    assert reloaded.priority == 2


def test_fetch_all_projects_excludes_inbox():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    client.create_card(name_to_id["Inbox"], "Raw idea")
    project = ProjectRecord(name="Real project", priority=1, status=ProjectStatus.NEW)
    sync_project_to_trello(client, project)

    projects = fetch_all_projects(client)

    assert [p.name for p in projects] == ["Real project"]


def test_sync_round_trips_provider_stop_reason_and_retry_after():
    client = InMemoryTrelloClient()
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.PAUSED,
        main_task="Build the thing",
        provider="claude",
        stop_reason="provider session limit hit",
        retry_after="2026-01-01T00:30:00+00:00",
    )

    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(created["id"]), id_to_name)

    assert reloaded.provider == "claude"
    assert reloaded.stop_reason == "provider session limit hit"
    assert reloaded.retry_after == "2026-01-01T00:30:00+00:00"
    assert reloaded.status == ProjectStatus.PAUSED


def test_free_text_notes_preserved_alongside_structured_block():
    client = InMemoryTrelloClient()
    project = ProjectRecord(name="Demo", priority=0, status=ProjectStatus.NEW, main_task="task")
    created = sync_project_to_trello(client, project, notes="Human summary here")

    card = client.get_card(created["id"])
    assert "Human summary here" in card["desc"]
    assert "PM-DATA" in card["desc"]
