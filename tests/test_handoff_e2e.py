"""End-to-end regression test for the real handoff contract: Project
Manager (Trello-backed project, scheduler tick) -> ai-orchestrator
autonomous CLI (--project/--goal/--spec/--agent, outbox result) ->
result folded back into the Project Manager and synced to Trello.

The fake ``subprocess_run`` below stands in for the real ai-orchestrator
process: it parses the same argv shape the real CLI expects and writes
its result to the outbox, exactly like the real process would - so this
test exercises the full real contract (argument shape, spec file,
outbox read-back) without spawning an actual subprocess.
"""

import json
import subprocess
from datetime import timedelta, datetime, timezone

from ai_project_manager.daemon import run_tick
from ai_project_manager.lock import ProjectLockManager
from ai_project_manager.models import ProjectRecord as _ProjectRecord, ProjectStatus
from ai_project_manager.orchestrator_runner import build_run_fn, parse_spec_markdown
from ai_project_manager.providers import ProviderRegistry, ProviderState
from ai_project_manager.trello_client import InMemoryTrelloClient
from ai_project_manager.trello_sync import build_list_maps, project_from_card, sync_project_to_trello


def ProjectRecord(*args, **kwargs):
    """Build repository-backed fixtures with an explicit identity label."""
    if "project_key" not in kwargs and kwargs.get("name"):
        kwargs["project_key"] = kwargs["name"]
    return _ProjectRecord(*args, **kwargs)


def _checkout(tmp_path, name):
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    return str(path)


def _args_to_dict(argv):
    result = {}
    it = iter(argv)
    for token in it:
        if token.startswith("--"):
            result[token[2:]] = next(it, None)
    return result


def _fake_ai_orchestrator(outbox_dir, responses):
    """A stand-in ai-orchestrator process: reads the --spec Markdown file,
    looks up a scripted response by project name, and writes it to the
    outbox tagged with this run's run_id - the real process's actual
    contract (see orchestrator_runner.py)."""
    calls = []

    def subprocess_run(command):
        args = _args_to_dict(command)
        calls.append(command)
        spec = parse_spec_markdown(open(args["spec"], encoding="utf-8").read())
        project_name = spec["project_name"]
        response = dict(responses[project_name])
        response["run_id"] = args["run-id"]

        slug = project_name.strip().lower().replace(" ", "-")
        outbox_dir.mkdir(parents=True, exist_ok=True)
        (outbox_dir / f"autonomous-{slug}.json").write_text(json.dumps(response), encoding="utf-8")
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    return subprocess_run, calls


def test_project_manager_hands_off_to_orchestrator_and_syncs_result_back(tmp_path):
    project = ProjectRecord(
        name="Dashboard",
        priority=3,
        status=ProjectStatus.READY,
        main_task="Build the orchestrator status dashboard",
        orchestrator_ready_task="Implement the live status widget",
        next_step="Wire up polling",
        checkpoint={"step": 1},
    )
    client = InMemoryTrelloClient()
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    registry = ProviderRegistry()
    registry.mark_available("claude")

    outbox_dir = tmp_path / "outbox"
    subprocess_run, calls = _fake_ai_orchestrator(
        outbox_dir,
        responses={
            "Dashboard": {
                "checkpoint": {"step": 2},
                "last_output": "wired up polling",
                "next_step": "add error states",
                "status": "in_progress",
            }
        },
    )

    run_fn = build_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={"Dashboard": _checkout(tmp_path, "dashboard-checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(outbox_dir),
        subprocess_run=subprocess_run,
    )

    outcome = run_tick(client, registry, run_fn, default_providers=["claude"], lock_manager=ProjectLockManager(), provider_state_path=str(tmp_path / "provider_state.json"))

    assert outcome.ran is True
    assert len(calls) == 1
    args = _args_to_dict(calls[0])
    assert args["project"] == str(tmp_path / "dashboard-checkout")
    assert args["goal"] == "Implement the live status widget"
    # PM never selects a concrete provider; AO receives the central broker
    # dispatch target and performs provider selection itself.
    assert args["agent"] == "provider-broker"
    assert args["run-id"]

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)

    assert reloaded.checkpoint == {"step": 2}
    assert reloaded.last_output == "wired up polling"
    assert reloaded.next_step == "add error states"
    assert reloaded.status == ProjectStatus.IN_PROGRESS
    assert reloaded.provider == "claude"


def test_project_manager_resumes_from_checkpoint_after_provider_limit_via_real_contract(tmp_path):
    project = ProjectRecord(
        name="Dashboard",
        priority=3,
        status=ProjectStatus.IN_PROGRESS,
        orchestrator_ready_task="Implement the live status widget",
        checkpoint={"step": 5},
    )
    client = InMemoryTrelloClient()
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    clock_holder = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    registry = ProviderRegistry(clock=lambda: clock_holder["now"])
    registry.mark_limited("claude", retry_after=timedelta(minutes=30), checkpoint={"step": 5})

    outbox_dir = tmp_path / "outbox"
    subprocess_run, calls = _fake_ai_orchestrator(
        outbox_dir,
        responses={"Dashboard": {"checkpoint": {"step": 6}, "status": "in_progress"}},
    )

    run_fn = build_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={"Dashboard": _checkout(tmp_path, "dashboard-checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(outbox_dir),
        subprocess_run=subprocess_run,
    )

    # Still within the limit window: no call is made at all.
    outcome_1 = run_tick(client, registry, run_fn, default_providers=["claude"], lock_manager=ProjectLockManager(), provider_state_path=str(tmp_path / "provider_state.json"))
    assert outcome_1.ran is False
    assert calls == []

    # Past retry_after: resumes automatically, carrying the checkpoint
    # through into the spec file handed to ai-orchestrator.
    clock_holder["now"] += timedelta(minutes=31)
    outcome_2 = run_tick(client, registry, run_fn, default_providers=["claude"], lock_manager=ProjectLockManager(), provider_state_path=str(tmp_path / "provider_state.json"))

    assert outcome_2.ran is True
    assert registry.get_status("claude").state == ProviderState.AVAILABLE
    spec = parse_spec_markdown(open(_args_to_dict(calls[0])["spec"], encoding="utf-8").read())
    assert spec["checkpoint"] == {"step": 5}

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.checkpoint == {"step": 6}


def test_full_visible_trello_dod_checklist_survives_to_completion(tmp_path):
    """Regression for run 97ff125c008d497989af07e068ae914e: a card whose
    *visible* description carries 8 Definition-of-Done items must hand
    off all 8 to ai-orchestrator (via the spec file), and once every item
    is reported completed the card must land in Hotovo/DONE showing all
    8 as checked - never reduced to a single generic line."""
    client = InMemoryTrelloClient()
    ready_list_id = client.get_list_id_by_name("Ready")
    dod_lines = "\n".join(f"- [ ] bod {i}" for i in range(1, 9))
    card = client.create_card(
        ready_list_id,
        "P4 - Full DoD",
        desc=f"CIL: overit checklist.\n\nDEFINITION OF DONE:\n{dod_lines}",
        labels=["P4", "P4 - Full DoD"],
    )

    id_to_name, _ = build_list_maps(client)
    parsed = project_from_card(card, id_to_name)
    assert len(parsed.dod) == 8
    assert [item.text for item in parsed.dod] == [f"bod {i}" for i in range(1, 9)]

    registry = ProviderRegistry()
    registry.mark_available("claude")

    outbox_dir = tmp_path / "outbox"
    subprocess_run, calls = _fake_ai_orchestrator(
        outbox_dir,
        responses={
            "Full DoD": {
                "checkpoint": {"completed_dod_indices": list(range(8))},
                "last_output": "all 8 items verified",
                "status": "completed",
            }
        },
    )

    run_fn = build_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={"P4 - Full DoD": _checkout(tmp_path, "checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(outbox_dir),
        subprocess_run=subprocess_run,
    )

    outcome = run_tick(
        client,
        registry,
        run_fn,
        default_providers=["claude"],
        lock_manager=ProjectLockManager(),
        provider_state_path=str(tmp_path / "provider_state.json"),
    )

    assert outcome.ran is True
    spec = parse_spec_markdown(open(_args_to_dict(calls[0])["spec"], encoding="utf-8").read())
    assert len(spec["definition_of_done"]) == 8

    reloaded_card = client.get_card(card["id"])
    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(reloaded_card, id_to_name)

    assert reloaded.status == ProjectStatus.TESTING
    assert len(reloaded.dod) == 8
    assert all(item.checked for item in reloaded.dod)

    visible = reloaded_card["desc"].split("<!-- PM-DATA", 1)[0]
    assert "Hotovo" not in visible

    # The implementation result is not allowed to close the card. Only a
    # separate accepted verdict from ai-orchestrator may do that.
    outcome = run_tick(
        client,
        registry,
        run_fn,
        audit_run_fn=lambda _project, _provider: {
            "verdict": "accepted",
            "evidence": "independent audit verified all 8 items",
        },
        default_providers=["claude"],
        lock_manager=ProjectLockManager(),
        provider_state_path=str(tmp_path / "provider_state.json"),
    )
    assert outcome.ran is True
    reloaded_card = client.get_card(card["id"])
    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(reloaded_card, id_to_name)
    assert reloaded.status == ProjectStatus.DONE
    visible = reloaded_card["desc"].split("<!-- PM-DATA", 1)[0]
    assert visible.count("- [x]") == 8
    for i in range(1, 9):
        assert f"bod {i}" in visible


def test_incomplete_dod_never_closes_the_card(tmp_path):
    """The mirror image of the regression above: if ai-orchestrator
    reports the run as completed but the checkpoint only confirms some of
    the checklist items, the card must stay open (never DONE) instead of
    silently closing on an unverified Definition of Done."""
    client = InMemoryTrelloClient()
    ready_list_id = client.get_list_id_by_name("Ready")
    dod_lines = "\n".join(f"- [ ] bod {i}" for i in range(1, 9))
    card = client.create_card(
        ready_list_id,
        "P4 - Partial DoD",
        desc=f"CIL: overit checklist.\n\nDEFINITION OF DONE:\n{dod_lines}",
        labels=["P4", "P4 - Partial DoD"],
    )

    registry = ProviderRegistry()
    registry.mark_available("claude")

    outbox_dir = tmp_path / "outbox"
    subprocess_run, _calls = _fake_ai_orchestrator(
        outbox_dir,
        responses={
            "Partial DoD": {
                # Only 5 of the 8 items are confirmed - the run/agent
                # claims "completed" anyway.
                "checkpoint": {"completed_dod_indices": [0, 1, 2, 3, 4]},
                "last_output": "ran out of time on the last 3 items",
                "status": "completed",
            }
        },
    )

    run_fn = build_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={"P4 - Partial DoD": _checkout(tmp_path, "checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(outbox_dir),
        subprocess_run=subprocess_run,
    )

    outcome = run_tick(
        client,
        registry,
        run_fn,
        default_providers=["claude"],
        lock_manager=ProjectLockManager(),
        provider_state_path=str(tmp_path / "provider_state.json"),
    )

    assert outcome.ran is True
    reloaded_card = client.get_card(card["id"])
    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(reloaded_card, id_to_name)

    assert reloaded.status != ProjectStatus.DONE
    assert sum(1 for item in reloaded.dod if item.checked) == 5
    assert sum(1 for item in reloaded.dod if not item.checked) == 3
    assert "3" in reloaded.stop_reason


def test_project_manager_records_provider_limit_from_real_orchestrator_exit(tmp_path):
    project = ProjectRecord(
        name="Dashboard",
        priority=3,
        status=ProjectStatus.READY,
        orchestrator_ready_task="Implement the live status widget",
        checkpoint={"step": 1},
    )
    client = InMemoryTrelloClient()
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    registry = ProviderRegistry()
    registry.mark_available("claude")

    def subprocess_run(command):
        return subprocess.CompletedProcess(
            args=command, returncode=1, stdout="", stderr="Error: session limit exceeded, try later"
        )

    run_fn = build_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={"Dashboard": _checkout(tmp_path, "dashboard-checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(tmp_path / "outbox"),
        subprocess_run=subprocess_run,
    )

    outcome = run_tick(client, registry, run_fn, default_providers=["claude"], lock_manager=ProjectLockManager(), provider_state_path=str(tmp_path / "provider_state.json"))

    assert outcome.ran is True
    assert registry.get_status("claude").state == ProviderState.LIMITED

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status == ProjectStatus.PAUSED
    assert "session limit" in reloaded.stop_reason
