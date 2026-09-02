import json
import os
import sys
from pathlib import Path

import pytest

from ai_project_manager.card_contract import CURRENT_SCHEMA_VERSION, GOVERNANCE_POLICY
from ai_project_manager.cli import build_parser, main
from ai_project_manager.models import ProjectRecord, ProjectStatus
from ai_project_manager.trello_client import InMemoryTrelloClient
from ai_project_manager.trello_sync import (
    _parse_data_block,
    build_list_maps,
    project_from_card,
    sync_project_to_trello,
)


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    # main() persists provider state to a relative "provider_state.json"
    # (and would default spec_dir/outbox_dir under cwd too) unless
    # configured otherwise; chdir into a throwaway directory so a live
    # test run never writes into the real repo checkout.
    monkeypatch.chdir(tmp_path)


def test_parser_supports_once_flag():
    args = build_parser().parse_args(["--once"])
    assert args.once is True

    args = build_parser().parse_args([])
    assert args.once is False

    args = build_parser().parse_args(["--enable-inbox-intake"])
    assert args.enable_inbox_intake is True


def test_slack_probe_returns_delivery_status_without_loading_trello(monkeypatch):
    monkeypatch.setattr("ai_project_manager.cli.notify", lambda _message: True)
    for name in ("TRELLO_KEY", "TRELLO_TOKEN", "TRELLO_BOARD_ID"):
        monkeypatch.delenv(name, raising=False)

    assert main(["--slack-probe"]) == 0

    monkeypatch.setattr("ai_project_manager.cli.notify", lambda _message: False)
    assert main(["--slack-probe"]) == 1


def test_parser_supports_maintain_only_flag():
    args = build_parser().parse_args(["--maintain-only"])
    assert args.maintain_only is True

    args = build_parser().parse_args([])
    assert args.maintain_only is False


def test_parser_validates_and_normalizes_log_level():
    assert build_parser().parse_args(["--log-level", "debug"]).log_level == "DEBUG"

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--log-level", "verbose"])

    assert exc_info.value.code == 2


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
    monkeypatch.delenv("AI_PM_PROVIDER_MODELS", raising=False)


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


def test_main_maintain_only_migrates_and_notifies_without_ever_dispatching(monkeypatch):
    """DoD: a live migration/cleanup pass must go through the real
    ``main()`` entrypoint wiring, verifiably touch Trello (schema/identity
    migrated) and Slack (a notification sent), and never dispatch a
    project - the scheduler itself stays on HOLD."""
    _set_trello_env(monkeypatch)

    client = InMemoryTrelloClient()
    ready = client.get_list_id_by_name("Ready")
    legacy = client.create_card(ready, "P3 legacy card", desc="legacy free text", labels=["P3"])

    messages = []
    monkeypatch.setattr("ai_project_manager.daemon.notify", messages.append)

    dispatched = []

    def fake_run_fn(project, provider):
        dispatched.append(project.name)
        return {}

    exit_code = main(["--maintain-only"], client=client, run_fn=fake_run_fn)

    assert exit_code == 0
    assert dispatched == []

    migrated = client.get_card(legacy["id"])
    data = _parse_data_block(migrated["desc"])
    assert data["schema_version"] == CURRENT_SCHEMA_VERSION
    assert data["card_identity"]["card_id"] == legacy["id"]
    assert data["governance"] == GOVERNANCE_POLICY

    assert any("Živá údržba" in message for message in messages)


def test_main_maintain_only_applies_explicit_source_identity_migration(monkeypatch):
    """Maintenance must repair a stale generated Inbox identity without
    dispatching a provider or changing the card's priority/list."""
    _set_trello_env(monkeypatch)
    monkeypatch.setenv(
        "AI_PM_PROJECT_PATHS",
        json.dumps({"Station Agent": "D:/station-agent"}),
    )
    monkeypatch.setenv(
        "AI_PM_CARD_PROJECT_KEYS",
        json.dumps({"source-1": "Station Agent"}),
    )

    client = InMemoryTrelloClient()
    ready = client.get_list_id_by_name("Ready")
    card = client.create_card(
        ready,
        "P5.40 — oprava station agent [Inbox source-1]",
        desc="<!-- PM-DATA\n"
        + json.dumps(
            {
                "schema_version": CURRENT_SCHEMA_VERSION,
                "main_task": "station_agent auto tune",
                "project_key": "oprava station agent [Inbox source-1]",
                "inbox_preparation": {
                    "source_card_id": "source-1",
                    "generated_project": True,
                    "project_path": "D:/scratch/source-1",
                },
                "dod": [],
                "open_feedback": [],
                "checkpoint": {},
            }
        )
        + "\n-->",
        labels=["P5.40", "oprava station agent [Inbox source-1]"],
    )

    monkeypatch.setattr("ai_project_manager.daemon.notify", lambda message: None)
    assert main(["--maintain-only"], client=client, run_fn=lambda *_: (_ for _ in ()).throw(AssertionError())) == 0

    migrated = client.get_card(card["id"])
    assert [label["name"] for label in migrated["labels"]] == ["P5.40", "Station Agent"]
    assert migrated["list_id"] == ready


def test_main_maintain_only_reports_unsafe_card_without_overwriting_it(monkeypatch):
    _set_trello_env(monkeypatch)

    client = InMemoryTrelloClient()
    ready = client.get_list_id_by_name("Ready")
    bad_card = client.create_card(
        ready, "Wrong authority",
        desc='<!-- PM-DATA\n{"schema_version": 1, "checkpoint": {}, "dod": [], '
             '"open_feedback": [], "governance": {"source_of_truth": "local-files"}}\n-->',
    )
    before = client.get_card(bad_card["id"])

    messages = []
    monkeypatch.setattr("ai_project_manager.daemon.notify", messages.append)

    exit_code = main(["--maintain-only"], client=client, run_fn=lambda project, provider: {})

    assert exit_code == 1
    assert client.get_card(bad_card["id"]) == before
    assert any("Trello Card Contract" in message for message in messages)


def test_main_once_resolves_project_path_via_real_wiring_without_exact_title_override(monkeypatch, tmp_path):
    """Regression for the live failure this fix targets: a card like
    ``stop_reason: "cannot resolve local path ... configure
    AI_PM_PROJECT_PATHS ..."`` for Station Agent / "revize MD/JSON" work
    cards. This drives a real ``--once`` tick through ``main()``'s actual
    ``load_config`` -> ``build_run_fn`` -> ``resolve_project_path`` ->
    real ``subprocess.run`` -> outbox read-back wiring - the same code
    path a production run takes - with only the Trello client (swapped
    for the in-memory test double, same as every other test in this
    file) and the ai-orchestrator executable (swapped for a tiny local
    stub script) replaced so the test never makes a network call or
    spawns a real coding agent. ``AI_PM_PROJECT_PATHS`` below is keyed by
    stable identity only ("Station Agent"), deliberately never containing
    the card's actual current title, exactly mirroring the real
    ``scripts/run-ai-project-manager.ps1`` production config."""
    _set_trello_env(monkeypatch)
    # ``_isolate_cwd`` alone is not enough here: unlike every other test in
    # this file, this one lets main() build the *real* run_fn (run_fn=None
    # below), so load_config()'s AI_ORCHESTRATOR_SPEC_DIR/OUTBOX_DIR/
    # AI_PM_PROVIDER_STATE_PATH env vars - if already set in the ambient
    # environment (as they are in this project's own live scheduler setup)
    # - would silently win over the relative "specs"/"outbox" defaults and
    # read/write the real production directories instead of this test's
    # tmp_path. Pin all three explicitly so this test can never do that.
    monkeypatch.setenv("AI_ORCHESTRATOR_SPEC_DIR", str(tmp_path / "specs"))
    monkeypatch.setenv("AI_ORCHESTRATOR_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("AI_PM_PROVIDER_STATE_PATH", str(tmp_path / "provider_state.json"))

    outbox_dir = tmp_path / "outbox"
    stub = tmp_path / "fake_orchestrator.py"
    stub.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "outbox_dir = Path(sys.argv[1])\n"
        "opts = {}\n"
        "it = iter(sys.argv[2:])\n"
        "for token in it:\n"
        "    opts[token] = next(it, None)\n"
        "outbox_dir.mkdir(parents=True, exist_ok=True)\n"
        "payload = {\n"
        "    'status': 'completed',\n"
        "    'checkpoint': {'completed_dod_indices': [0]},\n"
        "    'run_id': opts['--run-id'],\n"
        "    'last_output': 'resolved project path: ' + opts['--project'],\n"
        "}\n"
        "(outbox_dir / ('autonomous-' + opts['--run-id'] + '.json')).write_text(json.dumps(payload), encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "AI_ORCHESTRATOR_CMD", f'"{sys.executable}" "{stub}" "{outbox_dir}"'
    )

    station_checkout = str(tmp_path / "station-agent-checkout")
    Path(station_checkout).mkdir()
    monkeypatch.setenv(
        "AI_PM_PROJECT_PATHS",
        json.dumps({"Station Agent": station_checkout}),
    )

    # The exact wording/prefix this card ships with today - deliberately
    # NOT present, in any form, as a key in AI_PM_PROJECT_PATHS above.
    card_title = "P0 — Station Agent: revize MD/JSON"
    project = ProjectRecord(
        name=card_title,
        priority=0,
        status=ProjectStatus.READY,
        orchestrator_ready_task="Revize MD/JSON",
        project_key="Station Agent",
    )
    client = InMemoryTrelloClient()
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    exit_code = main(["--once"], client=client)

    assert exit_code == 0

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status == ProjectStatus.TESTING
    assert reloaded.last_output == f"resolved project path: {station_checkout}"


@pytest.mark.parametrize(
    "card_title,project_key,expected_checkout_name",
    [
        ("P5 - Izolace testovacich Slack notifikaci", "AI Project Manager", "ai-project-manager-checkout"),
        ("P3 - Fix retry backoff in provider registry", "AI Orchestrator", "ai-orchestrator-checkout"),
        ("P1 - DX Cluster scoring propagation", "Station Agent", "station-agent-checkout"),
    ],
)
def test_main_once_resolves_project_path_via_stable_label_identity_without_title_phrase(
    monkeypatch, tmp_path, card_title, project_key, expected_checkout_name
):
    """Root-cause regression for the real live failure: the card "P5 -
    Izolace testovacich Slack notifikaci" has no trace of "AI Project
    Manager" anywhere in its title, so the title-phrase fallback this suite
    already covers (see the "Station Agent" test above) cannot resolve it -
    ``cannot resolve local path`` was the real reported failure. This
    drives a real ``--once`` tick through ``main()``'s actual
    ``load_config`` -> ``build_run_fn`` -> ``resolve_project_path`` ->
    real ``subprocess.run`` -> outbox read-back wiring for all three real
    repositories (AI Project Manager, AI Orchestrator, Station Agent),
    using only the card's ``project_key`` label - never its title - to
    resolve the checkout."""
    _set_trello_env(monkeypatch)
    monkeypatch.setenv("AI_ORCHESTRATOR_SPEC_DIR", str(tmp_path / "specs"))
    monkeypatch.setenv("AI_ORCHESTRATOR_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("AI_PM_PROVIDER_STATE_PATH", str(tmp_path / "provider_state.json"))

    outbox_dir = tmp_path / "outbox"
    stub = tmp_path / "fake_orchestrator.py"
    stub.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "outbox_dir = Path(sys.argv[1])\n"
        "opts = {}\n"
        "it = iter(sys.argv[2:])\n"
        "for token in it:\n"
        "    opts[token] = next(it, None)\n"
        "outbox_dir.mkdir(parents=True, exist_ok=True)\n"
        "payload = {\n"
        "    'status': 'completed',\n"
        "    'checkpoint': {'completed_dod_indices': [0]},\n"
        "    'run_id': opts['--run-id'],\n"
        "    'last_output': 'resolved project path: ' + opts['--project'],\n"
        "}\n"
        "(outbox_dir / ('autonomous-' + opts['--run-id'] + '.json')).write_text(json.dumps(payload), encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "AI_ORCHESTRATOR_CMD", f'"{sys.executable}" "{stub}" "{outbox_dir}"'
    )

    checkout = str(tmp_path / expected_checkout_name)
    Path(checkout).mkdir()
    # Keyed by each project's stable label identity only - deliberately
    # never containing this card's exact current title - mirroring the
    # real scripts/run-ai-project-manager.ps1 production config.
    monkeypatch.setenv(
        "AI_PM_PROJECT_PATHS",
        json.dumps({"AI Project Manager": checkout, "AI Orchestrator": checkout, "Station Agent": checkout}),
    )

    project = ProjectRecord(
        name=card_title,
        priority=0,
        status=ProjectStatus.READY,
        orchestrator_ready_task="Work item unrelated to the project's own name",
        project_key=project_key,
    )
    client = InMemoryTrelloClient()
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    exit_code = main(["--once"], client=client)

    assert exit_code == 0

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status == ProjectStatus.TESTING
    assert reloaded.last_output == f"resolved project path: {checkout}"


def test_main_once_migrates_and_resolves_real_production_card_without_exact_title_override(
    monkeypatch, tmp_path
):
    """Full regression for the real reported live failure (2026-08-26):
    the production card "P5 - Izolace testovacich Slack notifikaci" starts
    out with only a bare P5 priority label - the whole board has only
    P1/P4/P5 labels, no project identity label has ever existed - and its
    description names no project at all, so it has neither a project_key
    label nor any title/content phrase to match. This drives a real
    ``--once`` tick through ``main()``'s actual ``load_config`` ->
    ``load_projects_and_inbox`` (bootstrap/migration) -> ``build_run_fn``
    -> ``resolve_project_path`` -> real ``subprocess.run`` -> outbox
    read-back wiring, using ``AI_PM_CARD_PROJECT_KEYS`` (keyed by the
    card's immutable Trello ID) as the only migration input - deliberately
    with no ``AI_PM_PROJECT_PATHS`` entry keyed by this card's exact
    title."""
    _set_trello_env(monkeypatch)
    monkeypatch.setenv("AI_ORCHESTRATOR_SPEC_DIR", str(tmp_path / "specs"))
    monkeypatch.setenv("AI_ORCHESTRATOR_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("AI_PM_PROVIDER_STATE_PATH", str(tmp_path / "provider_state.json"))

    outbox_dir = tmp_path / "outbox"
    stub = tmp_path / "fake_orchestrator.py"
    stub.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "outbox_dir = Path(sys.argv[1])\n"
        "opts = {}\n"
        "it = iter(sys.argv[2:])\n"
        "for token in it:\n"
        "    opts[token] = next(it, None)\n"
        "outbox_dir.mkdir(parents=True, exist_ok=True)\n"
        "payload = {\n"
        "    'status': 'completed',\n"
        "    'checkpoint': {'completed_dod_indices': [0]},\n"
        "    'run_id': opts['--run-id'],\n"
        "    'last_output': 'resolved project path: ' + opts['--project'],\n"
        "}\n"
        "(outbox_dir / ('autonomous-' + opts['--run-id'] + '.json')).write_text(json.dumps(payload), encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "AI_ORCHESTRATOR_CMD", f'"{sys.executable}" "{stub}" "{outbox_dir}"'
    )

    checkout = str(tmp_path / "ai-project-manager-checkout")
    Path(checkout).mkdir()
    # Keyed only by the 3 stable project identities - deliberately no key
    # matching this card's exact current title anywhere.
    monkeypatch.setenv(
        "AI_PM_PROJECT_PATHS",
        json.dumps(
            {
                "AI Project Manager": checkout,
                "AI Orchestrator": str(tmp_path / "ai-orchestrator-checkout"),
                "Station Agent": str(tmp_path / "station-agent-checkout"),
            }
        ),
    )

    client = InMemoryTrelloClient()
    project = ProjectRecord(
        name="P5 — Izolace testovacích Slack notifikací",
        priority=5,
        status=ProjectStatus.READY,
        orchestrator_ready_task="Ověřit že testovací Slack zprávy nechodí do produkčního kanálu.",
    )
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]
    assert {label["name"] for label in created["labels"]} == {"P5"}

    monkeypatch.setenv(
        "AI_PM_CARD_PROJECT_KEYS",
        json.dumps({project.trello_card_id: "AI Project Manager"}),
    )

    exit_code = main(["--once"], client=client)

    assert exit_code == 0

    id_to_name, _ = build_list_maps(client)
    reloaded_card = client.get_card(project.trello_card_id)
    assert {label["name"] for label in reloaded_card["labels"]} == {"P5", "AI Project Manager"}

    reloaded = project_from_card(reloaded_card, id_to_name)
    assert reloaded.project_key == "AI Project Manager"
    assert reloaded.status == ProjectStatus.TESTING
    assert reloaded.last_output == f"resolved project path: {checkout}"


def test_main_once_migrates_real_production_card_via_title_keyed_config_matching_production_script(
    monkeypatch, tmp_path
):
    """Same real-card regression as above, but keyed by the card's exact
    title rather than its Trello ID - exactly the shape actually shipped
    in scripts/run-ai-project-manager.ps1's AI_PM_CARD_PROJECT_KEYS entry,
    since that script cannot know the card's internal Trello ID ahead of
    time and only ever sees its title."""
    _set_trello_env(monkeypatch)
    monkeypatch.setenv("AI_ORCHESTRATOR_SPEC_DIR", str(tmp_path / "specs"))
    monkeypatch.setenv("AI_ORCHESTRATOR_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("AI_PM_PROVIDER_STATE_PATH", str(tmp_path / "provider_state.json"))

    outbox_dir = tmp_path / "outbox"
    stub = tmp_path / "fake_orchestrator.py"
    stub.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "outbox_dir = Path(sys.argv[1])\n"
        "opts = {}\n"
        "it = iter(sys.argv[2:])\n"
        "for token in it:\n"
        "    opts[token] = next(it, None)\n"
        "outbox_dir.mkdir(parents=True, exist_ok=True)\n"
        "payload = {\n"
        "    'status': 'completed',\n"
        "    'checkpoint': {'completed_dod_indices': [0]},\n"
        "    'run_id': opts['--run-id'],\n"
        "    'last_output': 'resolved project path: ' + opts['--project'],\n"
        "}\n"
        "(outbox_dir / ('autonomous-' + opts['--run-id'] + '.json')).write_text(json.dumps(payload), encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "AI_ORCHESTRATOR_CMD", f'"{sys.executable}" "{stub}" "{outbox_dir}"'
    )

    checkout = str(tmp_path / "ai-project-manager-checkout")
    Path(checkout).mkdir()
    monkeypatch.setenv(
        "AI_PM_PROJECT_PATHS",
        json.dumps(
            {
                "AI Project Manager": checkout,
                "AI Orchestrator": str(tmp_path / "ai-orchestrator-checkout"),
                "Station Agent": str(tmp_path / "station-agent-checkout"),
            }
        ),
    )

    card_title = "P5 — Izolace testovacích Slack notifikací"
    monkeypatch.setenv(
        "AI_PM_CARD_PROJECT_KEYS",
        json.dumps({card_title: "AI Project Manager"}),
    )

    client = InMemoryTrelloClient()
    project = ProjectRecord(
        name=card_title,
        priority=5,
        status=ProjectStatus.READY,
        orchestrator_ready_task="Ověřit že testovací Slack zprávy nechodí do produkčního kanálu.",
    )
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]
    assert {label["name"] for label in created["labels"]} == {"P5"}

    exit_code = main(["--once"], client=client)

    assert exit_code == 0

    id_to_name, _ = build_list_maps(client)
    reloaded_card = client.get_card(project.trello_card_id)
    assert {label["name"] for label in reloaded_card["labels"]} == {"P5", "AI Project Manager"}

    reloaded = project_from_card(reloaded_card, id_to_name)
    assert reloaded.project_key == "AI Project Manager"
    assert reloaded.status == ProjectStatus.TESTING
    assert reloaded.last_output == f"resolved project path: {checkout}"


def test_main_wires_configured_orchestrator_timeout_into_build_run_fn(monkeypatch):
    """AI_ORCHESTRATOR_TIMEOUT_SECONDS must actually reach build_run_fn
    when main() builds the real run_fn (run_fn=None) - previously it was
    parsed into Config and then dropped on the floor."""
    _set_trello_env(monkeypatch)
    monkeypatch.setenv("AI_ORCHESTRATOR_TIMEOUT_SECONDS", "42")

    # No schedulable work, so the built run_fn is never actually called -
    # this isolates the wiring itself from needing to fake a subprocess.
    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.DONE)
    client = InMemoryTrelloClient()
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    seen = {}

    def fake_build_run_fn(provider_registry, **kwargs):
        seen.update(kwargs)
        return lambda project, provider: {}

    monkeypatch.setattr("ai_project_manager.cli.build_run_fn", fake_build_run_fn)

    exit_code = main(["--once"], client=client)

    assert exit_code == 0
    assert seen["timeout_seconds"] == 42.0


def test_main_wires_configured_artifact_cleanup_policy_into_run_loop(monkeypatch):
    """AI_PM_ARTIFACT_CLEANUP_ROOT/AI_PM_ARTIFACT_RETENTION_HOURS must
    actually reach run_loop when main() wires the real config - mirrors
    the AI_ORCHESTRATOR_TIMEOUT_SECONDS regression above, where a parsed
    Config field was silently dropped before reaching its consumer."""
    _set_trello_env(monkeypatch)
    monkeypatch.setenv("AI_PM_ARTIFACT_CLEANUP_ROOT", "test-artifacts")
    monkeypatch.setenv("AI_PM_ARTIFACT_RETENTION_HOURS", "2.5")

    seen = {}

    def fake_run_loop(*args, **kwargs):
        seen.update(kwargs)
        from ai_project_manager.runner import RunOutcome
        return RunOutcome(ran=False, reason="idle")

    monkeypatch.setattr("ai_project_manager.cli.run_loop", fake_run_loop)

    exit_code = main(["--once"], client=InMemoryTrelloClient(), run_fn=lambda project, provider: {})

    assert exit_code == 0
    assert seen["artifact_cleanup_root"] == os.path.abspath("test-artifacts")
    assert seen["artifact_cleanup_retention_seconds"] == 2.5 * 3600


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


def test_main_once_returns_nonzero_when_trello_tick_fails(monkeypatch):
    _set_trello_env(monkeypatch)

    class BrokenClient(InMemoryTrelloClient):
        def list_lists(self):
            raise RuntimeError("Trello unavailable")

    exit_code = main(
        ["--once"],
        client=BrokenClient(),
        run_fn=lambda project, provider: {},
    )

    assert exit_code == 1


def test_main_exits_with_restart_required_code_when_self_update_detected(monkeypatch):
    """cli.main must never restart itself in-process; it only ever signals
    the need for a restart via a dedicated exit code so a separate
    supervising watchdog process (watchdog.py) performs the actual
    restart - see self_update.py/daemon.run_loop."""
    _set_trello_env(monkeypatch)

    from ai_project_manager.runner import RunOutcome
    from ai_project_manager.self_update import RESTART_REQUIRED_EXIT_CODE

    def fake_run_loop(*args, **kwargs):
        return RunOutcome(ran=False, reason="self-update restart required: tests passed", restart_required=True)

    monkeypatch.setattr("ai_project_manager.cli.run_loop", fake_run_loop)

    exit_code = main(["--once"], client=InMemoryTrelloClient(), run_fn=lambda project, provider: {})

    assert exit_code == RESTART_REQUIRED_EXIT_CODE
