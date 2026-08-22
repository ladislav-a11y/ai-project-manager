from ai_project_manager.cli import build_parser, main
from ai_project_manager.models import ProjectRecord, ProjectStatus
from ai_project_manager.trello_client import InMemoryTrelloClient
from ai_project_manager.trello_sync import build_list_maps, project_from_card, sync_project_to_trello


def test_parser_supports_once_flag():
    args = build_parser().parse_args(["--once"])
    assert args.once is True

    args = build_parser().parse_args([])
    assert args.once is False


def test_main_returns_error_code_when_required_config_missing(monkeypatch):
    for name in ("TRELLO_KEY", "TRELLO_TOKEN", "TRELLO_BOARD_ID"):
        monkeypatch.delenv(name, raising=False)

    exit_code = main(["--once"])

    assert exit_code == 2


def _set_trello_env(monkeypatch):
    monkeypatch.setenv("TRELLO_KEY", "test-key")
    monkeypatch.setenv("TRELLO_TOKEN", "test-token")
    monkeypatch.setenv("TRELLO_BOARD_ID", "test-board")
    monkeypatch.setenv("AI_PM_PROVIDERS", "claude")


def test_main_once_runs_a_full_tick_through_the_real_entrypoint_wiring(monkeypatch):
    """End-to-end: config -> provider registry -> scheduler loop -> run_fn
    -> Trello sync, all through ``main()`` itself (not just the library
    functions it calls), with only the Trello client and the
    ai-orchestrator dispatch swapped for test doubles so no network call
    happens."""
    _set_trello_env(monkeypatch)

    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY)
    client = InMemoryTrelloClient()
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    calls = []

    def fake_run_fn(project, provider):
        calls.append((project.name, provider))
        return {"status": "in_progress", "last_output": "done via entrypoint"}

    exit_code = main(["--once"], client=client, run_fn=fake_run_fn)

    assert exit_code == 0
    assert calls == [("Demo", "claude")]

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.last_output == "done via entrypoint"
    assert reloaded.provider == "claude"


def test_main_once_does_nothing_and_never_calls_run_fn_when_no_work(monkeypatch):
    _set_trello_env(monkeypatch)

    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.DONE)
    client = InMemoryTrelloClient()
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    calls = []

    def fake_run_fn(project, provider):
        calls.append((project.name, provider))
        return {}

    exit_code = main(["--once"], client=client, run_fn=fake_run_fn)

    assert exit_code == 0
    assert calls == []
