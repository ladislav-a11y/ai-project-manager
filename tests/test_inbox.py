import pytest
from pathlib import Path

from ai_project_manager.inbox import (
    PROCESSED_MARKER,
    apply_classification,
    classify_inbox_card,
    find_inbox_receipt,
    inbox_planner_providers,
    inbox_content_hash,
    inbox_source_reference,
    looks_like_feedback,
    process_inbox,
)
from ai_project_manager.inbox_preparation import (
    PreparedTask,
    derive_priority,
    enforce_indivisible_inbox_source_contract,
    is_explicit_indivisible_inbox_source,
    prepare_inbox_card,
    prepared_task_handoff_text,
    prioritize_inbox_cards,
    task_execution_order,
    visible_inbox_description,
)
from ai_project_manager.card_contract import dod_contract_issues
from ai_project_manager.models import ProjectRecord, ProjectStatus
from ai_project_manager.trello_client import InMemoryTrelloClient
from ai_project_manager.trello_sync import build_list_maps, fetch_all_projects, sync_project_to_trello


def test_inbox_planner_never_selects_retired_or_reserved_providers():
    assert inbox_planner_providers(["hermes", "gemini", "antigravity", "claude", "codex"]) == (
        "antigravity",
        "claude",
        "codex",
    )


def test_visible_inbox_description_fails_closed_on_unterminated_pm_data():
    card = {
        "name": "Human request",
        "desc": "Keep this text.\n<!-- PM-DATA\n{\"old\": \"history\"}",
    }

    assert visible_inbox_description(card) == "Keep this text."


def test_batch_prioritization_puts_pm_repairs_before_new_features():
    cards = [
        {"id": "feature", "name": "Nová funkce dashboardu", "desc": "Přidat nový widget"},
        {"id": "pm-bug", "name": "Oprava AI Project Manageru", "desc": "scheduler nefunguje a je potřeba opravit bug"},
    ]

    priorities = prioritize_inbox_cards(cards)

    assert priorities["pm-bug"][0] == 5
    assert priorities["feature"][0] == 3
    assert "závazná nejvyšší priorita" in priorities["pm-bug"][1]


def test_indivisible_source_contract_accepts_exactly_one_task():
    task = PreparedTask(
        title="scope", task="Udělat věc.", next_step="Začít.", scope="scope",
    )
    assert enforce_indivisible_inbox_source_contract((task,), indivisible=True) is None
    assert enforce_indivisible_inbox_source_contract((task,), indivisible=False) is None


def test_indivisible_source_contract_rejects_zero_tasks_regardless_of_marker():
    assert enforce_indivisible_inbox_source_contract((), indivisible=True) is not None
    assert enforce_indivisible_inbox_source_contract((), indivisible=False) is not None
    assert enforce_indivisible_inbox_source_contract("not a list", indivisible=False) is not None


def test_indivisible_source_contract_rejects_multiple_tasks_only_when_marked():
    task = PreparedTask(
        title="scope", task="Udělat věc.", next_step="Začít.", scope="scope",
    )
    # An ordinary splittable source is free to have several planned tasks.
    assert enforce_indivisible_inbox_source_contract((task, task), indivisible=False) is None
    # A source explicitly marked indivisible stays bound to exactly one.
    reason = enforce_indivisible_inbox_source_contract((task, task), indivisible=True)
    assert reason is not None
    assert "nedělitelná" in reason


def test_explicit_indivisible_marker_is_detected_in_title_or_description():
    assert is_explicit_indivisible_inbox_source({"name": "Nápad [indivisible]", "desc": ""})
    assert is_explicit_indivisible_inbox_source({"name": "Nápad", "desc": "Text. [Indivisible]"})
    assert not is_explicit_indivisible_inbox_source({"name": "Nápad", "desc": "Běžný požadavek."})


def test_process_inbox_allows_multiple_tasks_for_ordinary_splittable_source():
    """An ordinary Inbox source card (no explicit ``[indivisible]`` marker)
    may be split by the AI planner into several dependency-ordered tasks."""
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)

    source = client.create_card(
        name_to_id["Inbox"],
        "nový nápad",
        desc="Popis nápadu, který AI planner smí rozdělit na víc úkolů.",
    )

    def planner(card, projects):
        return {
            "provider": "claude",
            "model": None,
            "tasks": (
                PreparedTask(
                    title="část 1", task="Udělat první část.", next_step="Začít.",
                    scope="část 1",
                ),
                PreparedTask(
                    title="část 2", task="Udělat druhou část.", next_step="Pokračovat.",
                    scope="část 2",
                ),
            ),
        }

    changed = process_inbox(
        client,
        [],
        persist_project=lambda project: sync_project_to_trello(client, project),
        projects_root="D:/orchestrator",
        planner=planner,
    )

    assert changed != []
    assert client.get_card(source["id"])["closed"] is True
    assert client.list_cards(name_to_id["Inbox"]) == []
    assert len(client.list_cards(name_to_id["New"])) == 2
    assert all(project.trello_card_id != source["id"] for project in changed)


def test_process_inbox_keeps_source_card_immutable_while_preparing_children():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    source = client.create_card(
        name_to_id["Inbox"],
        "Station agent lookup",
        desc="Dohledat chybějící DXCC údaje z ověřitelných zdrojů.",
        labels=["Station Agent", "P5"],
    )
    source_before = {
        "name": source["name"],
        "desc": source["desc"],
        "list_id": source["list_id"],
        "labels": [label["name"] for label in source["labels"]],
    }

    changed = process_inbox(
        client,
        [],
        persist_project=lambda project: sync_project_to_trello(client, project),
        project_paths={"Station Agent": "D:/station-agent"},
    )

    stored = client.get_card(source["id"])
    assert {
        "name": stored["name"],
        "desc": stored["desc"],
        "list_id": stored["list_id"],
        "labels": [label["name"] for label in stored["labels"]],
    } == source_before
    assert stored["closed"] is True
    assert changed
    assert all(project.trello_card_id != source["id"] for project in changed)
    assert all(
        client.get_card(project.trello_card_id)["list_id"] == name_to_id["New"]
        for project in changed
    )


def test_process_inbox_fails_closed_when_marked_indivisible_source_returns_multiple_tasks():
    """Fail-closed indivisible Inbox source contract: an AI plan describing
    more than one task for a card explicitly marked ``[indivisible]`` must
    never reach Připraveno; the card stays in Inbox with no project written."""
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)

    source = client.create_card(
        name_to_id["Inbox"],
        "nový nápad [indivisible]",
        desc="Popis nápadu, který AI planner nesprávně rozdělí na víc úkolů.",
    )

    def planner(card, projects):
        return {
            "provider": "claude",
            "model": None,
            "tasks": (
                PreparedTask(
                    title="část 1", task="Udělat první část.", next_step="Začít.",
                    scope="část 1",
                ),
                PreparedTask(
                    title="část 2", task="Udělat druhou část.", next_step="Pokračovat.",
                    scope="část 2",
                ),
            ),
        }

    changed = process_inbox(
        client,
        [],
        persist_project=lambda project: sync_project_to_trello(client, project),
        projects_root="D:/orchestrator",
        planner=planner,
    )

    assert changed == []
    assert client.get_card(source["id"])["closed"] is False
    assert [card["id"] for card in client.list_cards(name_to_id["Inbox"])] == [source["id"]]


def test_ai_planner_cannot_raise_normal_feature_above_source_priority():
    prepared = prepare_inbox_card(
        {
            "id": "cw-source",
            "name": "CW dekodér – Windows desktopová aplikace",
            "desc": "Vytvořit novou Windows desktopovou aplikaci.",
            "labels": [],
        },
        projects_root="D:/projects",
        allow_new_project=True,
        planned_tasks=(
            PreparedTask(
                "architektura",
                "Navrhnout architekturu aplikace.",
                "Sepsat rozhraní modulů.",
                "architektura",
                priority=5.7,
                priority_reason="AI planner bez konkrétního důvodu",
            ),
        ),
    )

    assert prepared.priority == 2
    assert prepared.tasks[0].priority == 2
    assert "omezeno na prioritu zdrojového zadání" in prepared.tasks[0].priority_reason


def test_ai_planner_cannot_demote_corrective_source_priority():
    prepared = prepare_inbox_card(
        {
            "id": "ao-repair",
            "name": "P5 — AI Orchestrator audit nesouladu a reroute",
            "desc": "Opravit chybný audit a reroute v AI Orchestratoru.",
            "labels": [
                {"name": "AI Orchestrator"},
                {"name": "P5"},
            ],
        },
        planned_tasks=(
            PreparedTask(
                "audit",
                "Provést audit nesouladu a připravit reroute.",
                "Prověřit auditní tok.",
                "AI Orchestrator",
                priority=4.0,
                priority_reason="AI planner navrhl P4",
            ),
        ),
    )

    assert prepared.priority == 5
    assert prepared.tasks[0].priority == 5


def test_priority_rubric_handles_pm_abbreviation_and_functional_display_change():
    assert derive_priority(
        {"labels": [{"name": "AI Project Manager"}]},
        "Úprava zpráv PM do Slacku",
    )[0] == 5
    assert derive_priority(
        {"labels": [{"name": "Station Agent"}]},
        "band-opening: zobrazovat pouze jednu notifikaci",
    )[0] == 3
    assert derive_priority(
        {"labels": [{"name": "Station Agent"}]},
        "propagation: uvést debug info a průběžně vypočítat score",
    )[0] == 3
    assert derive_priority({}, "filtrovat podle ceny, lokality a dopravy")[0] == 2


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
    assert any(task.scope == "auto tune a hold" and task.priority == 5 for task in prepared.tasks)
    assert len({task.priority for task in prepared.tasks}) > 1
    assert all(item.phase in {"implementation", "audit"} for item in prepared.dod)
    assert sum(item.phase == "implementation" for item in prepared.dod) == 1
    assert sum(item.phase == "audit" for item in prepared.dod) == 2
    assert any(item.phase == "audit" for item in prepared.dod)
    assert dod_contract_issues(prepared.dod) == []


def test_documentation_task_gets_relevance_based_audit_not_mandatory_test_and_live_gates():
    prepared = prepare_inbox_card(
        {
            "id": "docs-source",
            "name": "AI Project Manager README documentation",
            "desc": "Do README přidat jednu přesnou větu.",
            "labels": [{"name": "AI Project Manager"}],
        },
        project_paths={"AI Project Manager": "D:/pm"},
        planned_tasks=(
            PreparedTask(
                title="README documentation",
                task="Přidat přesnou větu do README.md.",
                next_step="Upravit pouze README.md.",
                scope="README documentation",
                project_key="AI Project Manager",
            ),
        ),
    )

    audit_items = [item.text for item in prepared.dod if item.phase == "audit"]
    assert len(audit_items) == 2
    evidence_item, verdict_item = audit_items
    assert "relevantních pro povahu změny" in evidence_item
    assert "podle potřeby" in evidence_item
    assert "nerelevantní typ důkazu není povinný" in evidence_item
    assert "accepted / rejected" in verdict_item
    assert not any(
        text.startswith("Nezávislý audit ai-orchestratoru provede cílené regresní testy")
        for text in audit_items
    )
    assert not any(
        text.startswith("Nezávislý audit ai-orchestratoru ověří relevantní chování v živém prostředí")
        for text in audit_items
    )
    assert dod_contract_issues(prepared.dod) == []


def test_read_only_research_task_gets_audit_only_dod_and_verification_handoff():
    task = PreparedTask(
        title="README verification",
        task=(
            "Check that the paragraph appears exactly once in README.md; "
            "do not modify any files."
        ),
        next_step="Only verify the existing README.",
        scope="README verification",
        priority=2,
        priority_reason="ověření",
        project_key="AI Project Manager",
        work_type="research",
        split_reason=(
            "Verification is a separate atomic task that must not modify files."
        ),
    )

    prepared = prepare_inbox_card(
        {
            "id": "read-only-source",
            "name": "README verification",
            "desc": "Verify the existing README without changes.",
            "labels": [{"name": "AI Project Manager"}],
        },
        project_paths={"AI Project Manager": "D:/pm"},
        planned_tasks=(task,),
    )

    assert not any(item.phase == "implementation" for item in prepared.dod)
    assert all(item.phase == "audit" for item in prepared.dod)
    assert "read-only" in prepared.dod[0].text
    assert prepared_task_handoff_text(task, "AI Project Manager").startswith("Ověřit")
    assert dod_contract_issues(prepared.dod) == []


def test_process_inbox_materializes_read_only_research_as_audit_only_child():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    source = client.create_card(
        name_to_id["Inbox"],
        "Verify README update",
        desc="Verify the existing README without changes.",
        labels=["AI Project Manager"],
    )
    task = PreparedTask(
        title="README verification",
        task="Check README.md exactly once; do not modify any files.",
        next_step="Only verify the existing README.",
        scope="README verification",
        project_key="AI Project Manager",
        work_type="research",
        split_reason="A separate verification task that must not modify files.",
    )

    def planner(card, projects):
        return {"provider": "groq", "model": "test", "tasks": (task,)}

    changed = process_inbox(
        client,
        [],
        persist_project=lambda project: sync_project_to_trello(client, project),
        project_paths={"AI Project Manager": "D:/pm"},
        planner=planner,
    )

    assert len(changed) == 1
    project = changed[0]
    assert project.trello_card_id != source["id"]
    assert project.status == ProjectStatus.NEW
    assert project.orchestrator_ready_task.startswith("Ověřit")
    assert all(item.phase == "audit" for item in project.dod)
    assert client.get_card(project.trello_card_id)["list_id"] == name_to_id["New"]


def test_subtask_routes_to_its_own_project_identity_instead_of_source():
    """Regression for the fixed root cause (was: diagnostic-only).

    ``resolve_project_key`` still runs once against the whole source card
    to bound priority and to fail closed on an ambiguous/missing identity,
    but each ``PreparedTask`` now carries its own resolved ``project_key``.
    A trailing/remainder subtask whose own content unambiguously names a
    *different* configured project is routed there instead of blindly
    inheriting the source card's identity (here "Station Agent"), while an
    undifferentiated subtask still inherits the source identity.
    """
    card = {
        "id": "mixed-source",
        "name": "Station agent oprava a rozšíření",
        "desc": (
            "Auto tune a hold nefunguje. "
            "Opravit dokumentaci AI Project Manageru v README souboru."
        ),
        "labels": [{"name": "Station Agent"}],
    }

    prepared = prepare_inbox_card(
        card,
        project_paths={"Station Agent": "D:/station-agent", "AI Project Manager": "D:/pm"},
    )

    assert len(prepared.tasks) == 2
    first_task, last_task = prepared.tasks
    assert "AI Project Manager" in last_task.task
    assert last_task.scope != "auto tune a hold"
    # The undifferentiated subtask still inherits the source identity...
    assert first_task.project_key == "Station Agent"
    # ...but the subtask naming a different configured project is routed
    # by its own scope instead of the source card's identity.
    assert last_task.project_key == "AI Project Manager"
    # The source-level identity (used for priority bounds, generation, and
    # fail-closed checks) remains the whole card's own resolution.
    assert prepared.project_key == "Station Agent"


def test_ambiguous_repair_requires_explicit_project_identity():
    card = {
        "id": "station-live-source",
        "name": "oprava kritické aplikace",
        "desc": (
            "Station Agent má regresi a AI Project Manager ji musí opravit; "
            "nejdříve potvrdit, kterého projektu se karta týká."
        ),
    }

    prepared = prepare_inbox_card(
        card,
        project_paths={
            "Station Agent": "D:/station-agent",
            "AI Project Manager": "D:/pm",
        },
        projects_root="D:/orchestrator",
        allow_new_project=True,
    )

    assert prepared.project_key is None
    assert prepared.generated_project is False
    assert prepared.project_path is None
    assert prepared.human_required_reason


def test_station_title_identity_wins_over_cause_mentioned_in_description():
    card = {
        "id": "station-repair-source",
        "name": "P5 — Oprava Station Agenta – aplikace nejde spustit",
        "desc": "Kvůli chybám AI Project Manageru se Station Agent rozbil.",
    }

    prepared = prepare_inbox_card(
        card,
        project_paths={
            "Station Agent": "D:/station-agent",
            "AI Project Manager": "D:/pm",
        },
        projects_root="D:/orchestrator",
        allow_new_project=True,
    )

    assert prepared.project_key == "Station Agent"
    assert prepared.generated_project is False
    assert prepared.project_path is None
    assert prepared.human_required_reason is None


def test_large_inbox_split_uses_unique_decimal_subpriorities_without_flattening_bands():
    card = {
        "id": "large-source",
        "name": "Budoucí projekt — katalog služeb",
        "desc": (
            "Cíl: katalog nabídek. Vyhledávání podle ceny. Deduplikace nabídek. "
            "AI připraví popis. Publikace na více platformách. Upozornění přes Slack. "
            "Budoucí technická rešerše."
        ),
    }

    prepared = prepare_inbox_card(card, projects_root="D:/orchestrator", allow_new_project=True)

    priorities = [task.priority for task in prepared.tasks]
    assert len(priorities) == len(set(priorities))
    assert any(priority != int(priority) for priority in priorities)
    assert max(priorities) <= 5


def test_task_execution_order_respects_dependencies_before_priority():
    card = {
        "id": "dependency-source",
        "name": "Navazující Inbox práce",
        "desc": "Základ. Závislá rozšířená část.",
    }
    prepared = prepare_inbox_card(
        card,
        projects_root="D:/orchestrator",
        allow_new_project=True,
        planned_tasks=(
            PreparedTask(
                title="základ", task="Připravit základ.", next_step="Prověřit základ.",
                scope="základ", priority=1.0, depends_on=(),
            ),
            PreparedTask(
                title="rozšíření", task="Provést rozšíření.", next_step="Navázat na základ.",
                scope="rozšíření", priority=5.0, depends_on=(0,),
            ),
        ),
    )
    assert task_execution_order(prepared.tasks) == (0, 1)


def test_task_execution_order_rejects_dependency_cycle():
    from ai_project_manager.inbox_preparation import PreparedTask

    tasks = (
        PreparedTask("a", "a", "a", "a", priority=1, depends_on=(1,)),
        PreparedTask("b", "b", "b", "b", priority=2, depends_on=(0,)),
    )
    with pytest.raises(ValueError, match="cycle"):
        task_execution_order(tasks)


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
    assert client.get_card(source["id"])["list_id"] == name_to_id["Inbox"]
    prepared_card = client.get_card(changed[0].trello_card_id)
    assert prepared_card["id"] != source["id"]
    assert prepared_card["name"] == "P5 — Úprava zpráv PM do slacku"
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
    assert all(project.trello_card_id != source["id"] for project in changed)
    assert client.get_card(source["id"])["closed"] is True
    assert client.list_cards(name_to_id["Inbox"]) == []
    assert len(client.list_cards(name_to_id["New"])) == 3
    for project in changed:
        metadata = project.extra_data["inbox_preparation"]
        assert metadata["source_card_id"] == source["id"]
        assert metadata["content_sha256"] == inbox_content_hash(source)
        assert metadata["scope"]
        assert metadata["source_priority"] == 5
        assert metadata["task_priority"] == project.priority
        assert metadata["dod"] == [item.to_dict() for item in project.dod]


def test_process_inbox_routes_infra_subtask_away_from_station_agent_source_identity():
    """Regression for the reported card: 3 Station Agent tasks + 1 PM/AO fix.

    A Station Agent-labelled source card that splits into three
    Station-Agent-scoped subtasks plus one subtask naming a different
    configured project (here the AI Orchestrator infrastructure) must not
    let the infra subtask inherit the source card's "Station Agent"
    identity: it gets its own resolved ``project_key``, and the Trello
    label/child-project path created for it must reflect that (not
    Station Agent).
    """
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    source = client.create_card(
        name_to_id["Inbox"],
        "Station agent live chyby a rozšíření",
        desc=(
            "Bearing a vzdálenost. Auto tune a hold nefunguje. "
            "Přidat DX cluster poskytovatele. "
            "Opravit chybu v AI Orchestrator, který nesprávně routuje "
            "inbox podúkoly na chybný projekt."
        ),
        labels=["Station Agent"],
    )

    changed = process_inbox(
        client,
        [],
        persist_project=lambda project: sync_project_to_trello(client, project),
        project_paths={"Station Agent": "D:/station-agent", "AI Orchestrator": "D:/ai-orchestrator"},
    )

    assert len(changed) == 4
    station_agent_tasks = [p for p in changed if p.project_key == "Station Agent"]
    infra_tasks = [p for p in changed if p.project_key == "AI Orchestrator"]
    assert len(station_agent_tasks) == 3
    assert len(infra_tasks) == 1
    infra_task = infra_tasks[0]
    assert infra_task.project_key != "Station Agent"
    assert "AI Orchestrator" in infra_task.main_task

    infra_card = client.get_card(infra_task.trello_card_id)
    infra_labels = {label["name"] for label in infra_card["labels"]}
    assert "AI Orchestrator" in infra_labels
    assert "Station Agent" not in infra_labels

    for task in station_agent_tasks:
        card = client.get_card(task.trello_card_id)
        labels = {label["name"] for label in card["labels"]}
        assert "Station Agent" in labels


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

    assert client.get_card(source["id"])["closed"] is False
    assert [card["id"] for card in client.list_cards(name_to_id["Inbox"])] == [source["id"]]
    partial = fetch_all_projects(client, exclude_list_names=("Inbox",))
    assert len(partial) == 1
    # The source may be restored/edited while a partial split is waiting for
    # retry; the identity must be recovered from the already durable child,
    # not guessed from title similarity.
    client.update_card(source["id"], labels=[])

    changed = process_inbox(
        client,
        partial,
        persist_project=lambda project: sync_project_to_trello(client, project),
        project_paths={"Station Agent": "D:/station-agent"},
    )

    assert len({project.trello_card_id for project in changed}) == 3
    assert client.get_card(source["id"])["closed"] is True
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
    first = process_inbox(
        client,
        projects,
        persist_project=lambda project: sync_project_to_trello(client, project),
    )
    second = process_inbox(
        client,
        fetch_all_projects(client),
        persist_project=lambda project: sync_project_to_trello(client, project),
    )
    changed = first + second

    names = {p.name for p in changed}
    assert "Orchestrator Dashboard" in names
    assert "Brand new weather widget idea" in names

    dashboard = next(p for p in changed if p.name == "Orchestrator Dashboard")
    assert any("spinner" in fb for fb in dashboard.open_feedback)

    new_project = next(p for p in changed if p.name == "Brand new weather widget idea")
    assert new_project.status == ProjectStatus.NEW
    assert new_project.priority == 2


def test_process_inbox_admits_only_highest_priority_source_card_per_tick():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    high = client.create_card(
        name_to_id["Inbox"],
        "P5 — Kritická oprava aplikace",
        desc="Opravit potvrzenou regresi.",
        labels=["Station Agent", "P5"],
    )
    low = client.create_card(
        name_to_id["Inbox"],
        "P2 — Budoucí nápad",
        desc="Připravit budoucí rozšíření.",
        labels=["Weather Widget", "P2"],
    )

    changed = process_inbox(
        client,
        [],
        persist_project=lambda project: sync_project_to_trello(client, project),
        project_paths={
            "Station Agent": "D:/station-agent",
            "Weather Widget": "D:/weather-widget",
        },
    )

    assert changed
    assert all(project.trello_card_id != high["id"] for project in changed)
    assert client.get_card(high["id"])["closed"] is True
    assert [card["id"] for card in client.list_cards(name_to_id["Inbox"])] == [low["id"]]
    assert all(
        project.extra_data["inbox_preparation"]["source_card_id"] == high["id"]
        for project in changed
    )


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
    assert created_card_id != inbox_card["id"]
    assert client.get_card(inbox_card["id"])["closed"] is True
    assert client.list_cards(name_to_id["Inbox"]) == []

    project.last_output = "first autonomous result"
    sync_project_to_trello(client, project)

    project_cards = [
        card
        for card in client.list_cards(name_to_id["New"])
        if card["id"] == created_card_id
    ]
    assert [card["id"] for card in project_cards] == [created_card_id]


def test_unlabelled_new_inbox_idea_is_prepared_as_isolated_prioritized_project(tmp_path):
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    source = client.create_card(
        name_to_id["Inbox"],
        "Budoucí projekt — Bazar Scout a multi-inzerce [VYSOKÁ PRIORITA]",
        desc="Získávat nabídky, rozdělit více inzerátů a ověřit export.",
    )
    project_paths = {}

    changed = process_inbox(
        client,
        [],
        persist_project=lambda project: sync_project_to_trello(client, project),
        project_paths=project_paths,
        projects_root=str(tmp_path / "projects"),
    )

    assert len(changed) >= 1
    assert client.get_card(source["id"])["closed"] is True
    assert client.list_cards(name_to_id["Inbox"]) == []
    ready_cards = client.list_cards(name_to_id["New"])
    assert len(ready_cards) == len(changed)
    assert all(card["name"].startswith("P") for card in ready_cards)
    for project in changed:
        metadata = project.extra_data["inbox_preparation"]
        assert metadata["source_card_id"] == source["id"]
        assert metadata["generated_project"] is True
        assert Path(metadata["project_path"]).is_dir()
        assert project.project_key in project_paths
        assert project_paths[project.project_key] == metadata["project_path"]


def test_generated_inbox_project_mapping_is_rehydrated_after_restart(tmp_path):
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    client.create_card(name_to_id["Inbox"], "New isolated catalog idea", desc="Build a catalog export")
    root = tmp_path / "projects"
    first_paths = {}

    process_inbox(
        client,
        [],
        persist_project=lambda project: sync_project_to_trello(client, project),
        project_paths=first_paths,
        projects_root=str(root),
    )
    second_paths = {}
    from ai_project_manager.daemon import load_projects_and_inbox

    loaded = load_projects_and_inbox(
        client,
        project_paths=second_paths,
        projects_root=str(root),
    )

    generated = next(project for project in loaded if project.extra_data.get("inbox_preparation", {}).get("generated_project"))
    assert generated.project_key in second_paths
    assert Path(second_paths[generated.project_key]).resolve() == Path(
        generated.extra_data["inbox_preparation"]["project_path"]
    ).resolve()


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
    target = next(
        project
        for project in projects
        if project.extra_data.get("inbox_preparation", {}).get("source_card_id") == first["id"]
    )

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
    target = next(
        project
        for project in projects
        if project.extra_data.get("inbox_preparation", {}).get("source_card_id") == source["id"]
    )

    revised = client.update_card(source["id"], desc="Build a weather widget with a forecast")
    # Simulate a source card that remained in the board Inbox until the
    # previous write completed; the ID is stable while its content changes.
    client.update_card(revised["id"], list_id=name_to_id["Inbox"])
    client._cards[revised["id"]]["closed"] = False
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

def test_process_inbox_uses_planner_for_new_work_on_existing_project():
    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)

    existing = ProjectRecord(
        name="Station Agent",
        priority=2,
        status=ProjectStatus.IN_PROGRESS,
        main_task="Existing Station Agent work",
        project_key="Station Agent",
    )

    source = client.create_card(
        name_to_id["Inbox"],
        "oprava station agent",
        desc="Station Agent does not work; replace mock behavior with real behavior.",
        labels=["Station Agent"],
    )

    planner_calls = []

    def planner(card, projects):
        planner_calls.append(card["id"])
        return {
            "provider": "claude",
            "model": None,
            "tasks": (
                PreparedTask(
                    title="Station Agent - real backend",
                    task="Replace mock behavior with real backend behavior.",
                    next_step="Implement real backend.",
                    scope="Station Agent real backend",
                    priority=5,
                    priority_reason="confirmed repair",
                ),
            ),
        }

    changed = process_inbox(
        client,
        [existing],
        persist_project=lambda project: sync_project_to_trello(client, project),
        project_paths={"Station Agent": "D:/station-agent"},
        planner=planner,
    )

    assert planner_calls == [source["id"]]
    assert len(changed) == 1
    assert changed[0] is not existing
    assert changed[0].project_key == "Station Agent"
    assert changed[0].status == ProjectStatus.NEW
    assert changed[0].trello_card_id != source["id"]
    assert changed[0].extra_data["inbox_preparation"]["source_card_id"] == source["id"]
    assert client.get_card(source["id"])["closed"] is True
    assert client.list_cards(name_to_id["Inbox"]) == []
    assert client.get_card(changed[0].trello_card_id)["list_id"] == name_to_id["New"]


def test_planner_project_key_prevents_generated_checkout_for_known_project(tmp_path):
    """Regression for the controlled Groq Inbox failure.

    A source card may be unlabelled while the AI planner correctly resolves
    the task to an existing configured project. PM must preserve that
    ``project_key`` and must not replace it with a generated source-title
    checkout.
    """
    from ai_project_manager.orchestrator_runner import resolve_project_path

    client = InMemoryTrelloClient()
    _, name_to_id = build_list_maps(client)
    source = client.create_card(
        name_to_id["Inbox"],
        "Test Groq intake – krátké atomické subtasky",
        desc=(
            "Projekt D:/orchestrator/ai-project-manager. "
            "Do README přidat jednu krátkou větu."
        ),
    )
    expected_path = r"D:\orchestrator\ai-project-manager"
    project_paths = {"AI Project Manager": expected_path}

    def planner(card, projects):
        return {
            "provider": "groq",
            "model": "openai/gpt-oss-120b",
            "tasks": (
                PreparedTask(
                    title="README věta",
                    task="Přidat požadovanou větu do README.",
                    next_step="Upravit pouze README.",
                    scope="README",
                    priority=2.2,
                    priority_reason="malá atomická změna",
                    project_key="AI Project Manager",
                    work_type="implementation",
                    split_reason="první atomický krok",
                ),
            ),
        }

    changed = process_inbox(
        client,
        [],
        persist_project=lambda project: sync_project_to_trello(client, project),
        project_paths=project_paths,
        projects_root=str(tmp_path / "generated-projects"),
        planner=planner,
    )

    assert len(changed) == 1
    project = changed[0]
    assert project.project_key == "AI Project Manager"
    assert resolve_project_path(project, project_paths=project_paths) == expected_path
    metadata = project.extra_data["inbox_preparation"]
    assert metadata["work_type"] == "implementation"
    assert metadata["split_reason"] == "první atomický krok"
    assert metadata["generated_project"] is False
    assert metadata["project_path"] is None
    card = client.get_card(project.trello_card_id)
    labels = {label["name"] for label in card["labels"]}
    assert "AI Project Manager" in labels
    assert not (tmp_path / "generated-projects").exists()
