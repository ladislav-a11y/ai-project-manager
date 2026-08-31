import pytest

from ai_project_manager.inbox import (
    PROCESSED_MARKER,
    apply_classification,
    classify_inbox_card,
    find_inbox_receipt,
    inbox_content_hash,
    inbox_source_reference,
    looks_like_feedback,
    process_inbox,
)
from ai_project_manager.inbox_preparation import prepare_inbox_card, prioritize_inbox_cards
from ai_project_manager.models import ProjectRecord, ProjectStatus
from ai_project_manager.trello_client import InMemoryTrelloClient
from ai_project_manager.trello_sync import build_list_maps, fetch_all_projects, sync_project_to_trello


def test_batch_prioritization_puts_pm_repairs_before_new_features():
    cards = [
        {"id": "feature", "name": "Nová funkce dashboardu", "desc": "Přidat nový widget"},
        {"id": "pm-bug", "name": "Oprava AI Project Manageru", "desc": "scheduler nefunguje a je potřeba opravit bug"},
    ]

    priorities = prioritize_inbox_cards(cards)

    assert priorities["pm-bug"][0] == 5
    assert priorities["feature"][0] == 3
    assert "oprava vlastního PM" in priorities["pm-bug"][1]


def test_preparation_splits_station_agent_card_and_assigns_each_scope_priority():
    card = {
        "id": "station-source",
        "name": "Station agent live chyby a rozšíření",
        "desc": "Bearing a vzdálenost. Auto tune a hold nefunguje. Přidat DX cluster poskytovatele.",
        "labels": [{"name": "Station Agent"}],
    }

    prepared = prepare_inbox_card(card, project_paths={"Station Agent": "D:/station-agent"})

    assert prepared.project_key == "Station Agent"
    assert len(prepared.tasks) == 3
    assert any(task.scope == "auto tune a hold" and task.priority == 4 for task in prepared.tasks)
    assert all(item.phase in {"implementation", "audit"} for item in prepared.dod)
    assert any(item.phase == "audit" for item in prepared.dod)


def test_preparation_ignores_stale_pm_data_and_uses_visible_request():
    card = {
        "id": "pm-source",
        "name": "Úprava zpráv PM do slacku",
        "desc": (
            "<!-- PM-DATA\n"
            '{"main_task": "propagation scoring", "open_feedback": []}\n-->'
        ),
        "labels": [{"name": "AI Project Manager"}, {"name": "P0"}],
    }

    prepared = prepare_inbox_card(card, project_paths={"AI Project Manager": "D:/pm"})

    assert len(prepared.tasks) == 1
    assert prepared.tasks[0].task == "Úprava zpráv PM do slacku."
    assert prepared.priority == 0


def test_process_inbox_moves_contract_only_card_to_ready_with_priority_title():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    source = client.create_card(
        name_to_id["Inbox"],
        "Úprava zpráv PM do slacku",
        desc=(
            "<!-- PM-DATA\n"
            '{"main_task": "propagation scoring", "open_feedback": []}\n-->'
        ),
        labels=["AI Project Manager", "P0"],
    )

    changed = process_inbox(
        client,
        [],
        persist_project=lambda project: sync_project_to_trello(client, project),
        project_paths={"AI Project Manager": "D:/pm"},
    )

    assert len(changed) == 1
    prepared_card = client.get_card(source["id"])
    assert prepared_card["name"] == "P0 — Úprava zpráv PM do slacku"
    assert prepared_card["list_id"] == name_to_id["New"]
    assert "propagation scoring" not in prepared_card["desc"]


def test_process_inbox_creates_multiple_ready_tasks_from_one_source_card():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    source = client.create_card(
        name_to_id["Inbox"],
        "Station agent live chyby a rozšíření",
        desc="Bearing a vzdálenost. Auto tune a hold nefunguje. Přidat DX cluster poskytovatele.",
        labels=["Station Agent"],
    )

    changed = process_inbox(
        client,
        [],
        persist_project=lambda project: sync_project_to_trello(client, project),
        project_paths={"Station Agent": "D:/station-agent"},
    )

    assert len(changed) == 3
    assert all(project.project_key == "Station Agent" for project in changed)
    assert all(project.status == ProjectStatus.NEW for project in changed)
    assert all(project.trello_card_id for project in changed)
    assert changed[0].trello_card_id == source["id"]
    assert client.list_cards(name_to_id["Inbox"]) == []
    assert len(client.list_cards(name_to_id["New"])) == 3
    for project in changed:
        metadata = project.extra_data["inbox_preparation"]
        assert metadata["source_card_id"] == source["id"]
        assert metadata["content_sha256"] == inbox_content_hash(source)
        assert metadata["scope"]
        assert metadata["source_priority"] == 4
        assert metadata["task_priority"] == project.priority
        assert metadata["dod"] == [item.to_dict() for item in project.dod]


def test_split_retry_keeps_source_until_children_persist_and_does_not_duplicate_them():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    source = client.create_card(
        name_to_id["Inbox"],
        "Station agent live chyby a rozšíření",
        desc="Bearing a vzdálenost. Auto tune nefunguje. Přidat DX cluster poskytovatele.",
        labels=["Station Agent"],
    )
    writes = 0

    def fail_on_second_child(project):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("transient persistence failure")
        return sync_project_to_trello(client, project)

    with pytest.raises(RuntimeError, match="transient persistence failure"):
        process_inbox(
            client,
            [],
            persist_project=fail_on_second_child,
            project_paths={"Station Agent": "D:/station-agent"},
        )

    assert [card["id"] for card in client.list_cards(name_to_id["Inbox"])] == [source["id"]]
    partial = fetch_all_projects(client, exclude_list_names=("Inbox",))
    assert len(partial) == 1

    changed = process_inbox(
        client,
        partial,
        persist_project=lambda project: sync_project_to_trello(client, project),
        project_paths={"Station Agent": "D:/station-agent"},
    )

    assert len({project.trello_card_id for project in changed}) == 3
    assert client.list_cards(name_to_id["Inbox"]) == []
    assert len(client.list_cards(name_to_id["New"])) == 3


def test_classify_matches_existing_project_by_keyword_overlap():
    projects = [
        ProjectRecord(name="Orchestrator Dashboard", main_task="React dashboard for orchestrator status"),
        ProjectRecord(name="Billing Service", main_task="Stripe billing integration"),
    ]
    card = {"id": "c1", "name": "Dashboard spinner bug", "desc": "The orchestrator dashboard spinner never stops"}

    result = classify_inbox_card(card, projects)

    assert result.is_new_project is False
    assert result.project_name == "Orchestrator Dashboard"
    assert result.confidence > 0


def test_classify_creates_new_project_when_no_match():
    projects = [ProjectRecord(name="Billing Service", main_task="Stripe billing integration")]
    card = {"id": "c2", "name": "Totally unrelated new idea", "desc": "Build a weather widget"}

    result = classify_inbox_card(card, projects)

    assert result.is_new_project is True
    assert result.project_name == "Totally unrelated new idea"


def test_classify_never_merges_new_inbox_work_into_terminal_done_project():
    projects = [
        ProjectRecord(
            name="P1 — Audit and stabilization ai-orchestrator",
            status=ProjectStatus.DONE,
            main_task="Station Agent audit and stabilization",
        )
    ]
    card = {
        "id": "c-done",
        "name": "Station Agent new live-test requirements",
        "desc": "Add bearing and distance to the selected station",
    }

    result = classify_inbox_card(card, projects)

    assert result.is_new_project is True
    assert result.project_name == card["name"]


def test_looks_like_feedback_detects_bug_language():
    assert looks_like_feedback("This button nefunguje spravne") is True
    assert looks_like_feedback("Add a new export feature") is False


def test_process_inbox_assigns_cards_to_projects_end_to_end():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)

    existing = ProjectRecord(
        name="Orchestrator Dashboard",
        priority=2,
        status=ProjectStatus.IN_PROGRESS,
        main_task="React dashboard for orchestrator status",
    )
    sync_project_to_trello(client, existing)

    client.create_card(
        name_to_id["Inbox"],
        "Dashboard spinner bug",
        desc="The orchestrator dashboard spinner never stops, this is a bug",
    )
    client.create_card(name_to_id["Inbox"], "Brand new weather widget idea", desc="Build a weather widget", labels=["Weather Widget"])

    projects = fetch_all_projects(client)
    changed = process_inbox(client, projects)

    names = {p.name for p in changed}
    assert "Orchestrator Dashboard" in names
    assert "Brand new weather widget idea" in names

    dashboard = next(p for p in changed if p.name == "Orchestrator Dashboard")
    assert any("spinner" in fb for fb in dashboard.open_feedback)

    new_project = next(p for p in changed if p.name == "Brand new weather widget idea")
    assert new_project.status == ProjectStatus.NEW
    assert new_project.priority == 2


def test_persisted_new_inbox_project_reuses_created_card_on_next_sync():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    inbox_card = client.create_card(
        name_to_id["Inbox"],
        "Brand new weather widget idea",
        desc="Build a weather widget",
        labels=["Weather Widget"],
    )

    changed = process_inbox(
        client,
        [],
        persist_project=lambda project: sync_project_to_trello(client, project),
    )
    project = changed[0]
    created_card_id = project.trello_card_id
    assert created_card_id == inbox_card["id"]
    assert client.list_cards(name_to_id["Inbox"]) == []

    project.last_output = "first autonomous result"
    sync_project_to_trello(client, project)

    project_cards = [
        card
        for card in client.list_cards(name_to_id["New"])
        if card["id"] == created_card_id
    ]
    assert [card["id"] for card in project_cards] == [created_card_id]


def test_existing_project_feedback_moves_source_to_done_and_is_retry_idempotent():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    existing = ProjectRecord(
        name="Orchestrator Dashboard",
        priority=4,
        status=ProjectStatus.READY,
        main_task="React dashboard for orchestrator status",
    )
    sync_project_to_trello(client, existing)
    source = client.create_card(
        name_to_id["Inbox"],
        "Dashboard spinner bug",
        desc="The orchestrator dashboard spinner never stops, this is a bug",
    )

    projects = fetch_all_projects(client)
    changed = process_inbox(
        client,
        projects,
        persist_project=lambda project: sync_project_to_trello(client, project),
    )

    assert client.list_cards(name_to_id["Inbox"]) == []
    receipt = client.get_card(source["id"])
    assert receipt["list_id"] == name_to_id["Done"]
    target = changed[0]
    assert target.extra_data["processed_inbox_card_ids"] == [source["id"]]
    assert len(target.open_feedback) == 1

    # Reapplying the same source receipt cannot duplicate the feedback.
    result = classify_inbox_card(source, [target])
    reapplied = apply_classification(source, result, {target.name: target})
    assert len(reapplied.open_feedback) == 1


def test_process_inbox_does_not_acknowledge_card_when_project_persistence_fails():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    inbox = client.create_card(
        name_to_id["Inbox"],
        "Brand new weather widget idea",
        desc="Build a weather widget",
        labels=["Weather Widget"],
    )

    def fail_persistence(project):
        raise RuntimeError("Trello project write failed")

    with pytest.raises(RuntimeError, match="project write failed"):
        process_inbox(client, [], persist_project=fail_persistence)

    stored = next(card for card in client.list_cards(name_to_id["Inbox"]) if card["id"] == inbox["id"])
    assert PROCESSED_MARKER not in stored["desc"]


def test_inbox_source_reference_is_stable_for_whitespace_and_case_changes():
    first = {"id": "source-1", "url": "https://trello.com/c/source-1", "name": "Fix BUG", "desc": "  Spinner  "}
    revised_formatting = {"id": "source-1", "url": "https://trello.com/c/source-1", "name": "fix bug", "desc": "Spinner"}

    assert inbox_content_hash(first) == inbox_content_hash(revised_formatting)
    reference = inbox_source_reference(first, target_card_id="target-1")
    assert reference["source_card_id"] == "source-1"
    assert reference["source_card_url"] == first["url"]
    assert reference["target_card_id"] == "target-1"


def test_inbox_receipt_matches_revised_source_by_id_before_hash():
    original = {"id": "source-1", "url": "https://trello.com/c/source-1", "name": "Original", "desc": "First request"}
    project = ProjectRecord(
        name="Target",
        extra_data={"inbox_receipts": [inbox_source_reference(original, target_card_id="target-1")]},
    )
    revised = {"id": "source-1", "url": original["url"], "name": "Original", "desc": "Revised request"}

    match = find_inbox_receipt([project], revised)

    assert match is not None
    assert match.project is project
    assert match.matched_by == "source_card_id_revision"
    assert match.receipt["content_sha256"] != inbox_content_hash(revised)


def test_duplicate_inbox_copy_creates_only_a_done_receipt_not_new_work():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    first = client.create_card(
        name_to_id["Inbox"],
        "Weather widget",
        desc="Build a weather widget",
        labels=["Weather Widget"],
    )

    process_inbox(
        client,
        [],
        persist_project=lambda project: sync_project_to_trello(client, project),
    )
    projects = fetch_all_projects(client)
    target = next(project for project in projects if project.trello_card_id == first["id"])

    duplicate = client.create_card(
        name_to_id["Inbox"],
        first["name"],
        desc=first["desc"],
    )
    process_inbox(
        client,
        projects,
        persist_project=lambda project: sync_project_to_trello(client, project),
    )

    work_cards = [
        card for card in client.list_all_cards()
        if card["list_id"] == name_to_id["New"] and card["id"] == target.trello_card_id
    ]
    duplicate_receipt = client.get_card(duplicate["id"])
    assert len(work_cards) == 1
    assert duplicate_receipt["list_id"] == name_to_id["Done"]
    assert "Duplicitní požadavek" in duplicate_receipt["desc"]


def test_revised_inbox_source_updates_existing_project_without_new_work_card():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    source = client.create_card(
        name_to_id["Inbox"],
        "Weather widget",
        desc="Build a weather widget",
        labels=["Weather Widget"],
    )
    process_inbox(
        client,
        [],
        persist_project=lambda project: sync_project_to_trello(client, project),
    )
    projects = fetch_all_projects(client)
    target = next(project for project in projects if project.trello_card_id == source["id"])

    revised = client.update_card(source["id"], desc="Build a weather widget with a forecast")
    # Simulate a source card that remained in the board Inbox until the
    # previous write completed; the ID is stable while its content changes.
    client.update_card(revised["id"], list_id=name_to_id["Inbox"])
    process_inbox(
        client,
        projects,
        persist_project=lambda project: sync_project_to_trello(client, project),
    )

    work_cards = [
        card for card in client.list_all_cards()
        if card["list_id"] == name_to_id["New"] and card["id"] == target.trello_card_id
    ]
    assert len(work_cards) == 1
    assert "forecast" in target.next_step
