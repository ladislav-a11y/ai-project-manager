from ai_project_manager.inbox import classify_inbox_card, looks_like_feedback, process_inbox
from ai_project_manager.models import ProjectRecord, ProjectStatus
from ai_project_manager.trello_client import InMemoryTrelloClient
from ai_project_manager.trello_sync import build_list_maps, fetch_all_projects, sync_project_to_trello


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
    client.create_card(name_to_id["Inbox"], "Brand new weather widget idea", desc="Build a weather widget")

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
