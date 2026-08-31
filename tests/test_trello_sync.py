import json
import pytest

from ai_project_manager.models import DoDItem, GitHubRef, GoogleDriveRef, ProjectRecord, ProjectStatus
from ai_project_manager.trello_client import InMemoryTrelloClient
from ai_project_manager.trello_sync import (
    _parse_data_block,
    _render_data_block,
    build_list_maps,
    card_updates_from_project,
    fetch_all_projects,
    maintain_board_contract,
    priority_from_labels,
    priority_from_card,
    project_from_card,
    project_key_from_labels,
    status_from_list,
    sync_project_to_trello,
)
from ai_project_manager.orchestrator_handoff import build_orchestrator_task
from ai_project_manager.card_contract import (
    CURRENT_SCHEMA_VERSION,
    GOVERNANCE_POLICY,
    CardContractError,
    UnsupportedCardSchemaError,
)


def test_priority_from_labels_reads_p_label():
    assert priority_from_labels([{"name": "P3"}]) == 3
    assert priority_from_labels([{"name": "bug"}, {"name": "P5"}]) == 5


def test_card_update_bounds_untrusted_audit_history_before_trello_write():
    client = InMemoryTrelloClient(("Připraveno", "Pracuje se", "Testování", "Hotovo"))
    card = client.create_card("list-2", "P5 — audit", labels=["P5", "APM"])
    project = project_from_card(card, {"list-2": "Pracuje se"})
    project.open_feedback = ["starý audit\n" * 5000]

    updates = card_updates_from_project(project, {"Pracuje se": "list-2"})

    assert len(updates["desc"]) <= 14000
    assert "starší auditní historie zkrácena" in updates["desc"]
    assert priority_from_labels([{"name": "bug"}]) == 0
    assert priority_from_labels([]) == 0


def test_card_identity_ignores_rich_text_url_whitespace():
    client = InMemoryTrelloClient(("Připraveno", "Pracuje se", "Testování", "Hotovo"))
    card = client.create_card("list-2", "P5 — URL", labels=["P5", "APM"])
    project = project_from_card(card, {"list-2": "Pracuje se"})
    project.trello_card_url = f"{card['url']} "

    sync_project_to_trello(client, project)

    assert client.get_card(card["id"])["name"] == project.name


def test_card_update_preserves_contract_when_visible_notes_are_oversized():
    client = InMemoryTrelloClient(("Připraveno", "Pracuje se", "Testování", "Hotovo"))
    card = client.create_card("list-2", "P5 — notes", labels=["P5", "APM"])
    project = project_from_card(card, {"list-2": "Pracuje se"})
    project.checkpoint = {"completed_dod_indices": [0], "run_id": "keep-me"}

    updates = card_updates_from_project(project, {"Pracuje se": "list-2"}, notes="n" * 20000)

    assert len(updates["desc"]) <= 14000
    assert "viditelná historie zkrácena" in updates["desc"]
    data = _parse_data_block(updates["desc"])
    assert data["checkpoint"]["run_id"] == "keep-me"


def test_priority_falls_back_to_card_title_when_board_has_no_priority_labels():
    assert priority_from_card({"name": "P5 — AI Orchestrator", "labels": []}) == 5
    assert priority_from_card({"name": "P1 - Audit", "labels": []}) == 1
    assert priority_from_card({"name": "No priority", "labels": []}) == 0


def test_non_terminal_work_card_name_always_exposes_current_priority():
    project = ProjectRecord(
        name="Inbox task",
        priority=3,
        status=ProjectStatus.READY,
        main_task="Implement the prepared task",
        extra_data={"inbox_preparation": {"source_card_id": "source-1"}},
    )

    updates = card_updates_from_project(project, {"Připraveno": "ready"})

    assert updates["name"] == "P3 — Inbox task"


def test_priority_prefix_is_replaced_after_reprioritization():
    project = ProjectRecord(
        name="P2 — Inbox task",
        priority=5,
        status=ProjectStatus.NEW,
        main_task="Repair the prepared task",
        extra_data={"inbox_preparation": {"source_card_id": "source-1"}},
    )

    updates = card_updates_from_project(project, {"Připraveno": "ready"})

    assert updates["name"] == "P5 — Inbox task"


def test_explicit_priority_label_wins_over_title_prefix():
    assert priority_from_card({"name": "P5 — title", "labels": [{"name": "P2"}]}) == 2


def test_project_key_from_labels_ignores_priority_labels():
    assert project_key_from_labels([{"name": "P3"}, {"name": "AI Project Manager"}]) == "AI Project Manager"
    assert project_key_from_labels([{"name": "AI Orchestrator"}, {"name": "P5"}]) == "AI Orchestrator"
    assert project_key_from_labels([{"name": "P0"}]) is None
    assert project_key_from_labels([]) is None
    assert project_key_from_labels(None) is None


def test_project_key_from_labels_rejects_ambiguous_identity():
    assert project_key_from_labels([
        {"name": "AI Project Manager"}, {"name": "AI Orchestrator"}, {"name": "P5"}
    ]) is None


def test_project_key_round_trips_through_sync_independent_of_title():
    """The project_key label - the card's stable identity - must survive
    a sync and be recoverable regardless of what the card's title says,
    which is the whole point: a card titled "Izolace testovacich Slack
    notifikaci" carries no trace of "AI Project Manager" in its title at
    all, only in this label."""
    client = InMemoryTrelloClient()
    project = ProjectRecord(
        name="P5 - Izolace testovacich Slack notifikaci",
        priority=5,
        status=ProjectStatus.READY,
        project_key="AI Project Manager",
    )

    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(created["id"]), id_to_name)

    assert reloaded.project_key == "AI Project Manager"
    assert reloaded.priority == 5


def test_project_key_label_survives_a_priority_or_status_change_sync():
    """card_updates_from_project must not silently drop the project_key
    label the next time it writes the card's labels back - previously only
    the priority label was ever written, which would wipe any other label
    (including a project-identity one) on the very next sync."""
    client = InMemoryTrelloClient()
    project = ProjectRecord(
        name="Demo",
        priority=1,
        status=ProjectStatus.NEW,
        project_key="Station Agent",
    )
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    project.priority = 4
    project.status = ProjectStatus.IN_PROGRESS
    sync_project_to_trello(client, project)

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(created["id"]), id_to_name)
    assert reloaded.project_key == "Station Agent"
    assert reloaded.priority == 4


def test_czech_board_lists_map_deterministically():
    maps = {
        "inbox": "INBOX / Nápady",
        "ready": "Připraveno",
        "working": "Pracuje se",
        "waiting": "Čeká na AI",
        "testing": "Testování",
        "done": "Hotovo",
    }
    by_id = {key: value for key, value in maps.items()}
    assert status_from_list("inbox", by_id) == ProjectStatus.INBOX
    assert status_from_list("ready", by_id) == ProjectStatus.READY
    assert status_from_list("working", by_id) == ProjectStatus.IN_PROGRESS
    assert status_from_list("waiting", by_id) == ProjectStatus.PAUSED
    assert status_from_list("testing", by_id) == ProjectStatus.TESTING
    assert status_from_list("done", by_id) == ProjectStatus.DONE


def test_unknown_trello_list_is_rejected_instead_of_becoming_new():
    import pytest

    with pytest.raises(ValueError, match="unmapped Trello list"):
        status_from_list("mystery", {"mystery": "Something new"})


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
    assert _parse_data_block(created["desc"])["schema_version"] == CURRENT_SCHEMA_VERSION


def test_legacy_unversioned_real_shape_migrates_without_losing_checkpoint_or_dod():
    legacy = {
        "checkpoint": {"completed_dod_indices": [0, 2], "run_id": "old"},
        "dod": [
            {"text": "usage kontrakt", "checked": True},
            {"text": "live provider", "checked": False},
        ],
        "lifecycle_status": "in_progress",
        "last_output": "historicky vystup",
        "legacy_extension": {"keep": True},
    }
    card = {
        "id": "legacy", "name": "P4 — Legacy", "list_id": "ready",
        "labels": [{"name": "P4"}],
        "desc": f"<!-- PM-DATA\n{json.dumps(legacy)}\n-->",
    }

    project = project_from_card(card, {"ready": "Připraveno"})

    assert project.checkpoint == {"completed_dod_indices": [0], "run_id": "old"}
    assert [item.checked for item in project.dod] == [True, False]
    assert project.extra_data == {"legacy_extension": {"keep": True}}


def test_legacy_dod_without_phase_is_classified_once_but_explicit_phase_is_preserved():
    legacy = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "checkpoint": {},
        "open_feedback": [],
        "governance": GOVERNANCE_POLICY,
        "lifecycle_status": "ready",
        "dod": [
            {
                "text": "Plná testovací sada projde a ai-orchestrator vydá accepted/rejected verdikt",
                "checked": False,
            },
            {
                "text": "implement audit logging",
                "checked": False,
                "phase": "implementation",
            },
        ],
    }
    card = {
        "id": "legacy-dod-phase",
        "name": "P2 - Legacy DoD phase",
        "list_id": "ready",
        "labels": [{"name": "P2"}],
        "desc": f"<!-- PM-DATA\n{json.dumps(legacy, ensure_ascii=False)}\n-->",
    }

    project = project_from_card(card, {"ready": "Připraveno"})

    assert [(item.text, item.phase) for item in project.dod] == [
        (
            "Plná testovací sada projde a ai-orchestrator vydá accepted/rejected verdikt",
            "audit",
        ),
        ("implement audit logging", "implementation"),
    ]

def test_unknown_compatible_fields_survive_round_trip():
    client = InMemoryTrelloClient()
    project = ProjectRecord(
        name="Demo", status=ProjectStatus.READY,
        extra_data={"future_optional": {"nested": [1, 2, 3]}},
    )
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]
    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(created, id_to_name)
    sync_project_to_trello(client, reloaded)

    raw = _parse_data_block(client.get_card(created["id"])["desc"])
    assert raw["future_optional"] == {"nested": [1, 2, 3]}


def test_legacy_learning_context_is_dropped_not_carried_forward():
    legacy = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "checkpoint": {},
        "dod": [],
        "open_feedback": [],
        "governance": GOVERNANCE_POLICY,
        "learning_context": {
            "version": 1,
            "current_strategy": "stale cross-card strategy",
            "history": [{"phase": "implementation", "result": "in_progress"}],
        },
    }

    client = InMemoryTrelloClient()
    id_to_name, name_to_id = build_list_maps(client)
    raw_card = client.create_card(
        name_to_id["Ready"], "P4 — Legacy learning", desc=f"<!-- PM-DATA\n{json.dumps(legacy)}\n-->",
        labels=["P4"],
    )

    project = project_from_card(raw_card, id_to_name)

    assert not hasattr(project, "learning_context")
    assert project.extra_data == {}

    sync_project_to_trello(client, project)
    raw = _parse_data_block(client.get_card(raw_card["id"])["desc"])
    assert "learning_context" not in raw


def test_future_schema_is_rejected_before_destructive_write():
    card = {
        "id": "future", "name": "Future", "list_id": "ready", "labels": [],
        "desc": '<!-- PM-DATA\n{"schema_version": 999, "checkpoint": {}, "dod": []}\n-->',
    }
    with pytest.raises(UnsupportedCardSchemaError, match="unsupported"):
        project_from_card(card, {"ready": "Připraveno"})


def test_conflicting_governance_is_rejected_without_write():
    client = InMemoryTrelloClient()
    ready = client.get_list_id_by_name("Ready")
    card = client.create_card(
        ready, "Wrong authority",
        desc='<!-- PM-DATA\n{"schema_version": 1, "checkpoint": {}, "dod": [], '
             '"open_feedback": [], "governance": {"source_of_truth": "local-files"}}\n-->',
    )
    before = client.get_card(card["id"])

    issues = maintain_board_contract(client)

    assert len(issues) == 1
    assert client.get_card(card["id"]) == before


def test_lifecycle_write_rejects_controller_verification_as_implementation_work():
    client = InMemoryTrelloClient()
    project = ProjectRecord(
        name="P5 — unsafe verification routing",
        status=ProjectStatus.READY,
        dod=[DoDItem(
            text=(
                "ai-orchestrator musí provést syntaxe -> cílené testy -> "
                "git --no-pager diff -> git --no-pager diff --check -> plný test suite"
            )
        )],
    )

    with pytest.raises(CardContractError, match="unsafe DoD routing"):
        sync_project_to_trello(client, project)

    assert client.list_cards(client.get_list_id_by_name("Ready")) == []


def test_maintenance_routes_completed_implementation_with_pending_audit_to_testing():
    client = InMemoryTrelloClient()
    project = ProjectRecord(
        name="P5 — completed implementation",
        status=ProjectStatus.READY,
        dod=[
            DoDItem(text="implementation", checked=True),
            DoDItem(text="independent audit accepts evidence", phase="audit"),
        ],
    )
    created = sync_project_to_trello(client, project)

    assert maintain_board_contract(client) == []

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(created["id"]), id_to_name)
    assert reloaded.status == ProjectStatus.TESTING
    assert reloaded.stop_reason == "implementation DoD complete; awaiting ai-orchestrator audit"


def test_maintenance_migrates_identity_governance_and_order_idempotently():
    client = InMemoryTrelloClient()
    ready = client.get_list_id_by_name("Ready")
    low = client.create_card(ready, "P1 low", desc="legacy", labels=["P1"])
    high = client.create_card(ready, "P5 high", desc="legacy", labels=["P5"])

    assert maintain_board_contract(client) == []
    ordered = client.list_cards(ready)
    assert [card["id"] for card in ordered] == [high["id"], low["id"]]
    for migrated in ordered:
        data = _parse_data_block(migrated["desc"])
        assert data["governance"] == GOVERNANCE_POLICY
        assert data["card_identity"]["card_id"] == migrated["id"]

    snapshot = client.list_cards(ready)
    assert maintain_board_contract(client) == []
    assert client.list_cards(ready) == snapshot


def test_maintenance_repairs_incomplete_current_contract_and_reads_back():
    client = InMemoryTrelloClient()
    ready = client.get_list_id_by_name("Ready")
    data = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "governance": GOVERNANCE_POLICY,
        "main_task": "Zachovat úkol",
        "checkpoint": {"run_id": "preserve"},
        "dod": [{"text": "ověřit", "checked": False}],
        # open_feedback is intentionally missing: this was the live P3
        # failure that previously made maintenance skip the card.
    }
    card = client.create_card(ready, "P3 incomplete", _render_data_block(data), labels=["P3"])

    assert maintain_board_contract(client) == []

    repaired = client.get_card(card["id"])
    repaired_data = _parse_data_block(repaired["desc"])
    assert repaired_data["open_feedback"] == []
    assert repaired_data["checkpoint"] == {"run_id": "preserve"}
    assert repaired_data["card_identity"]["card_id"] == card["id"]
    assert repaired_data["lifecycle_status"] == "ready"


def test_active_queue_orders_all_priorities_p5_to_p0():
    """DoD: an active queue always shows P5 (highest) down to P0 (lowest),
    regardless of the order cards were created in."""
    client = InMemoryTrelloClient()
    ready = client.get_list_id_by_name("Ready")
    cards = {
        priority: client.create_card(ready, f"P{priority} card", desc="legacy", labels=[f"P{priority}"])
        for priority in (2, 5, 0, 4, 1, 3)
    }

    assert maintain_board_contract(client) == []

    ordered = client.list_cards(ready)
    assert [card["id"] for card in ordered] == [cards[p]["id"] for p in (5, 4, 3, 2, 1, 0)]


def test_done_list_orders_chronologically_newest_first():
    """DoD: Hotovo/Done is ordered by completion date/time, newest first."""
    client = InMemoryTrelloClient()
    done = client.get_list_id_by_name("Done")

    def make_done_card(name: str, completed_at: str) -> dict:
        project = ProjectRecord(
            name=name,
            status=ProjectStatus.DONE,
            completed_at=completed_at,
            dod=[DoDItem(text="hotovo", checked=True)],
        )
        return sync_project_to_trello(client, project)

    oldest = make_done_card("Oldest", "2026-01-01T10:00:00+00:00")
    newest = make_done_card("Newest", "2026-03-01T10:00:00+00:00")
    middle = make_done_card("Middle", "2026-02-01T10:00:00+00:00")

    assert maintain_board_contract(client) == []

    ordered = client.list_cards(done)
    assert [card["id"] for card in ordered] == [newest["id"], middle["id"], oldest["id"]]


def test_physical_ready_list_overrides_stale_completed_contract():
    client = InMemoryTrelloClient()
    ready = client.get_list_id_by_name("Ready")
    data = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "governance": GOVERNANCE_POLICY,
        "checkpoint": {"completed_dod_indices": [0]},
        "dod": [{"text": "verified", "checked": True}],
        "open_feedback": [],
        "lifecycle_status": "done",
    }
    card = client.create_card(ready, "P4 completed", _render_data_block(data), labels=["P4"])

    assert maintain_board_contract(client) == []
    repaired = client.get_card(card["id"])
    assert repaired["list_id"] == ready
    repaired_data = _parse_data_block(repaired["desc"])
    assert repaired_data["lifecycle_status"] == "ready"


def test_new_card_is_immediately_bound_to_its_trello_identity():
    client = InMemoryTrelloClient()
    project = ProjectRecord(name="Identity", status=ProjectStatus.READY)

    created = sync_project_to_trello(client, project)
    raw = _parse_data_block(created["desc"])

    assert raw["card_identity"] == {
        "card_id": created["id"],
        "card_url": created["url"],
    }


def test_contract_bound_to_another_card_is_rejected_on_read():
    data = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "checkpoint": {}, "dod": [], "open_feedback": [],
        "lifecycle_status": "ready",
        "card_identity": {"card_id": "other", "card_url": "https://trello.com/c/other"},
    }
    card = {
        "id": "actual", "name": "Actual", "list_id": "ready", "labels": [],
        "url": "https://trello.com/c/actual",
        "desc": f"<!-- PM-DATA\n{json.dumps(data)}\n-->",
    }

    with pytest.raises(CardContractError, match="identity mismatch"):
        project_from_card(card, {"ready": "Připraveno"})


def test_sync_refuses_wrong_existing_card_id_when_expected_url_differs():
    client = InMemoryTrelloClient()
    ready = client.get_list_id_by_name("Ready")
    first = client.create_card(ready, "First")
    second = client.create_card(ready, "Second")
    project = ProjectRecord(
        name="First", status=ProjectStatus.READY,
        trello_card_id=second["id"], trello_card_url=first["url"],
    )

    with pytest.raises(CardContractError, match="refusing Trello write"):
        sync_project_to_trello(client, project)

    assert client.get_card(second["id"])["name"] == "Second"


def test_malformed_pm_data_is_not_silently_reset_to_empty_defaults():
    card = {
        "id": "broken", "name": "Broken", "list_id": "ready", "labels": [],
        "desc": "<!-- PM-DATA\n{not-json}\n-->",
    }
    with pytest.raises(CardContractError, match="invalid PM-DATA"):
        project_from_card(card, {"ready": "Připraveno"})


def test_recovery_attempts_and_review_at_round_trip_through_trello():
    """Unattended blocked-task recovery (recovery.py) must not lose its
    own state across a Trello sync - a fresh process re-reading the card
    needs the exact same attempt count and backoff deadline to keep the
    max-attempts loop guard working across restarts."""
    client = InMemoryTrelloClient()
    project = ProjectRecord(
        name="Widget",
        priority=2,
        status=ProjectStatus.BLOCKED,
        blocked_by="automatic recovery exhausted after 3 attempt(s)",
        recovery_attempts=3,
        review_at="2026-02-01T00:00:00+00:00",
    )

    created = sync_project_to_trello(client, project)
    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(created["id"]), id_to_name)

    assert reloaded.recovery_attempts == 3
    assert reloaded.review_at == "2026-02-01T00:00:00+00:00"


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


def test_done_card_keeps_human_visible_checked_dod_above_pm_data():
    client = InMemoryTrelloClient()
    task = (
        "CIL: opravit mapovani.\n"
        "DEFINITION OF DONE: [ ] pricina potvrzena [ ] testy projdou [ ] realny beh overen."
    )
    project = ProjectRecord(
        name="P5 — Mapovani",
        priority=5,
        status=ProjectStatus.DONE,
        main_task=task,
        orchestrator_ready_task=task,
        last_output="357 tests passed; live sync passed",
        checkpoint={"completed_dod_indices": [0, 1, 2]},
    )

    created = sync_project_to_trello(client, project)
    visible = created["desc"].split("<!-- PM-DATA", 1)[0]

    assert visible.count("- [x]") == 3
    assert "pricina potvrzena" in visible
    assert "357 tests passed" in visible
    assert "<!-- PM-DATA" in created["desc"]


def test_visible_checklist_above_pm_data_parses_into_project_dod():
    """A fresh card (never synced by the Project Manager yet) whose
    visible description carries a Definition-of-Done checklist must have
    every item parsed onto ProjectRecord.dod - not silently reduced to
    whatever ends up in a single structured field."""
    card = {
        "id": "card-1",
        "name": "P4 - Full DoD",
        "desc": (
            "CIL: overit DoD.\n\n"
            "DEFINITION OF DONE:\n"
            + "\n".join(f"- [ ] bod {i}" for i in range(1, 9))
        ),
        "list_id": "list-ready",
        "labels": [],
    }

    reloaded = project_from_card(card, {"list-ready": "Ready"})

    assert [item.text for item in reloaded.dod] == [f"bod {i}" for i in range(1, 9)]
    assert all(not item.checked for item in reloaded.dod)


def test_visible_checklist_precedes_structured_checklist_deterministically():
    """Order is not incidental: the visible checklist (what a human reads
    on the card) always comes first, then any structured-only item -
    never interleaved or reordered by set/dict iteration order."""
    data_block = json.dumps(
        {"orchestrator_ready_task": "- [ ] shared item\n- [ ] structured only"}
    )
    card = {
        "id": "card-1",
        "name": "Demo",
        "desc": (
            "DEFINITION OF DONE:\n"
            "- [ ] shared item\n"
            "- [ ] visible only\n\n"
            f"<!-- PM-DATA\n{data_block}\n-->"
        ),
        "list_id": "list-ready",
        "labels": [],
    }

    reloaded = project_from_card(card, {"list-ready": "Ready"})

    assert [item.text for item in reloaded.dod] == [
        "shared item",
        "visible only",
        "structured only",
    ]


def test_dod_survives_a_non_done_sync_that_blanks_the_visible_notes():
    """The visible checklist text is only re-derivable on the very first
    read - every later sync blanks the visible portion of the card for
    any non-DONE status. The parsed checklist must therefore be persisted
    in the structured block and round-trip intact regardless."""
    client = InMemoryTrelloClient()
    ready_list_id = client.get_list_id_by_name("Ready")
    card = client.create_card(
        ready_list_id,
        "Demo",
        desc="DEFINITION OF DONE:\n" + "\n".join(f"- [ ] bod {i}" for i in range(1, 9)),
    )

    id_to_name, _ = build_list_maps(client)
    project = project_from_card(client.get_card(card["id"]), id_to_name)
    project.trello_card_id = card["id"]
    project.status = ProjectStatus.IN_PROGRESS

    sync_project_to_trello(client, project)

    reloaded = project_from_card(client.get_card(card["id"]), id_to_name)
    assert [item.text for item in reloaded.dod] == [f"bod {i}" for i in range(1, 9)]


def test_visible_trello_audit_dod_is_excluded_from_implementation_handoff():
    """Exercise the production path, not a manually phase-tagged record."""
    data_block = json.dumps(
        {
            "main_task": "Implement the feature",
            "orchestrator_ready_task": "Implement the feature",
        }
    )
    card = {
        "id": "card-audit-dod",
        "name": "Demo",
        "desc": (
            "DEFINITION OF DONE:\n"
            "- [ ] implement audit logging\n"
            "- [ ] full tests and independent audit pass\n"
            "- [ ] plné testy a nezávislý audit projdou\n"
            f"<!-- PM-DATA\n{data_block}\n-->"
        ),
        "list_id": "list-ready",
        "labels": [],
    }

    project = project_from_card(card, {"list-ready": "Ready"})
    task = build_orchestrator_task(project)

    assert [(item.text, item.phase) for item in project.dod] == [
        ("implement audit logging", "implementation"),
        ("full tests and independent audit pass", "audit"),
        ("plné testy a nezávislý audit projdou", "audit"),
    ]
    assert task.definition_of_done == ["implement audit logging"]


def test_real_unicode_czech_audit_dod_is_excluded_from_implementation_handoff():
    """Regression for production Trello text, independent of console code pages."""
    czech_audit_item = "pln\u00e9 testy a nez\u00e1visl\u00fd audit projdou"
    card = {
        "id": "card-czech-audit-dod",
        "name": "Demo",
        "desc": (
            "DEFINITION OF DONE:\n"
            "- [ ] implementace je hotov\u00e1\n"
            f"- [ ] {czech_audit_item}\n"
        ),
        "list_id": "list-ready",
        "labels": [],
    }

    project = project_from_card(card, {"list-ready": "Ready"})
    task = build_orchestrator_task(project)

    assert [(item.text, item.phase) for item in project.dod] == [
        ("implementace je hotov\u00e1", "implementation"),
        (czech_audit_item, "audit"),
    ]
    assert task.definition_of_done == ["implementace je hotov\u00e1"]



def test_ai_orchestrator_verdict_dod_is_audit_phase():
    """A controller-owned accepted/rejected verdict must never enter implementation."""
    audit_item = (
        "Pln\u00e1 testovac\u00ed sada projde a ai-orchestrator "
        "vyd\u00e1 accepted/rejected verdikt"
    )
    card = {
        "id": "card-ai-orchestrator-verdict-dod",
        "name": "Demo",
        "desc": (
            "DEFINITION OF DONE:\n"
            "- [x] implementace je hotov\u00e1\n"
            f"- [ ] {audit_item}\n"
        ),
        "list_id": "list-ready",
        "labels": [],
    }

    project = project_from_card(card, {"list-ready": "Ready"})
    task = build_orchestrator_task(project)

    assert [(item.text, item.phase) for item in project.dod] == [
        ("implementace je hotov\u00e1", "implementation"),
        (audit_item, "audit"),
    ]
    assert task.definition_of_done == ["implementace je hotov\u00e1"]


def test_done_card_visible_dod_reflects_each_items_actual_checked_state():
    client = InMemoryTrelloClient()
    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.DONE,
        dod=[
            DoDItem(text="a", checked=True),
            DoDItem(text="b", checked=False),
            DoDItem(text="c", checked=True),
        ],
    )

    created = sync_project_to_trello(client, project)
    visible = created["desc"].split("<!-- PM-DATA", 1)[0]

    assert "- [x] a" in visible
    assert "- [ ] b" in visible
    assert "- [x] c" in visible


def test_done_card_with_unverified_dod_is_returned_to_testing():
    client = InMemoryTrelloClient()
    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.DONE,
        dod=[DoDItem(text="implemented", checked=True), DoDItem(text="tested", checked=False)],
        checkpoint={"completed_dod_indices": [0]},
    )
    created = sync_project_to_trello(client, project)

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(created["id"]), id_to_name)

    assert reloaded.status == ProjectStatus.TESTING
    assert reloaded.completed_at is None
    assert [item.checked for item in reloaded.dod] == [True, False]


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


def test_shared_waiting_list_round_trips_each_logical_status():
    """One Trello list can represent several non-runnable states."""
    list_names = ("INBOX / Nápady", "Připraveno", "Pracuje se", "Čeká na AI", "Hotovo")

    for status in (ProjectStatus.PAUSED, ProjectStatus.BLOCKED, ProjectStatus.ERROR):
        client = InMemoryTrelloClient(list_names)
        project = ProjectRecord(name=f"Demo {status.value}", status=status)
        created = sync_project_to_trello(client, project)
        id_to_name, _ = build_list_maps(client)

        reloaded = project_from_card(created, id_to_name)

        assert reloaded.status == status


def test_stale_structured_status_cannot_override_a_manual_list_move():
    client = InMemoryTrelloClient(("Připraveno", "Čeká na AI", "Hotovo"))
    project = ProjectRecord(name="Demo", status=ProjectStatus.PAUSED)
    created = sync_project_to_trello(client, project)
    done_list_id = client.get_list_id_by_name("Hotovo")
    moved = client.move_card(created["id"], done_list_id)
    id_to_name, _ = build_list_maps(client)

    reloaded = project_from_card(moved, id_to_name)

    assert reloaded.status == ProjectStatus.DONE


def test_waiting_visible_notes_show_reason_time_next_attempt_and_human_step():
    """DoD: a card sitting in 'Čeká na AI' must visibly state why it is
    waiting, since when, when the next automatic attempt happens, and
    what a human should do - without needing to inspect PM-DATA."""
    client = InMemoryTrelloClient()
    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.BLOCKED,
        blocked_by="missing credentials for deploy target",
        waiting_since="2026-01-01T10:00:00+00:00",
        retry_after="2026-01-02T10:00:00+00:00",
        next_step="Wait for ops to rotate the key",
        human_action_step="Add the API key to the vault and unblock the card",
    )

    created = sync_project_to_trello(client, project)
    visible = created["desc"].split("<!-- PM-DATA", 1)[0]

    assert "ČEKÁ NA AI" in visible
    assert "missing credentials for deploy target" in visible
    assert "Čeká od:" in visible
    assert "Další automatický pokus:" in visible
    assert "Wait for ops to rotate the key" in visible
    assert "Add the API key to the vault and unblock the card" in visible

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(created, id_to_name)
    assert reloaded.waiting_since == "2026-01-01T10:00:00+00:00"
    assert reloaded.retry_after == "2026-01-02T10:00:00+00:00"
    assert reloaded.human_action_step == "Add the API key to the vault and unblock the card"


def test_free_text_notes_preserved_alongside_structured_block():
    client = InMemoryTrelloClient()
    project = ProjectRecord(name="Demo", priority=0, status=ProjectStatus.NEW, main_task="task")
    created = sync_project_to_trello(client, project, notes="Human summary here")

    card = client.get_card(created["id"])
    assert "Human summary here" in card["desc"]
    assert "PM-DATA" in card["desc"]


def test_data_block_round_trips_a_literal_html_comment_close_in_field_values():
    """Regression: agent-generated text (last_output, a checkpoint value,
    stop_reason, ...) can very plausibly contain the literal substring
    "-->" (a diff, HTML/markdown, plain "before --> after" prose). Left
    unescaped, that used to prematurely close the fenced PM-DATA block,
    truncate the embedded JSON mid-way, fail to parse, and silently wipe
    every field the block carries back to defaults on the very next read
    - defeating the whole point of Trello being the durable source of
    truth for checkpoint resume."""
    data = {
        "checkpoint": {"diff": "line1\n--> arrow\nline3"},
        "last_output": "Applied patch: before --> after, also <!-- note -->",
        "stop_reason": "session limit: token --> exhausted",
    }

    block = _render_data_block(data)

    assert _parse_data_block(block) == data


def test_sync_project_to_trello_survives_arrow_sequences_in_every_free_text_field():
    client = InMemoryTrelloClient()
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.IN_PROGRESS,
        main_task="Build the thing",
        last_output="Applied patch: before --> after, also <!-- note -->",
        checkpoint={"diff": "line1\n--> arrow\nline3"},
        stop_reason="session limit: token --> exhausted",
    )

    created = sync_project_to_trello(client, project, notes="Human note with an arrow --> here too")
    project.trello_card_id = created["id"]

    card = client.get_card(created["id"])
    assert "Human note with an arrow --> here too" in card["desc"]

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(card, id_to_name)

    assert reloaded.last_output == project.last_output
    assert reloaded.checkpoint == project.checkpoint
    assert reloaded.stop_reason == project.stop_reason
    assert reloaded.main_task == project.main_task
