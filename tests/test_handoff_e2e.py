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
from ai_project_manager.models import ProjectRecord, ProjectStatus
from ai_project_manager.orchestrator_runner import build_run_fn
from ai_project_manager.providers import ProviderRegistry, ProviderState
from ai_project_manager.trello_client import InMemoryTrelloClient
from ai_project_manager.trello_sync import build_list_maps, project_from_card, sync_project_to_trello


def _args_to_dict(argv):
    result = {}
    it = iter(argv)
    for token in it:
        if token.startswith("--"):
            result[token[2:]] = next(it, None)
    return result


def _fake_ai_orchestrator(outbox_dir, responses):
    """A stand-in ai-orchestrator process: reads the --spec file, looks up
    a scripted response by project name, and writes it to the outbox -
    the real process's actual contract."""
    calls = []

    def subprocess_run(command):
        args = _args_to_dict(command)
        calls.append(command)
        spec = json.loads(open(args["spec"], encoding="utf-8").read())
        project_name = spec["project_name"]
        response = responses[project_name]

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
        project_paths={"Dashboard": str(tmp_path / "dashboard-checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(outbox_dir),
        subprocess_run=subprocess_run,
    )

    outcome = run_tick(client, registry, run_fn, default_providers=["claude"], lock_manager=ProjectLockManager())

    assert outcome.ran is True
    assert len(calls) == 1
    args = _args_to_dict(calls[0])
    assert args["project"] == str(tmp_path / "dashboard-checkout")
    assert args["goal"] == "Implement the live status widget"
    assert args["agent"] == "claude"

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
        project_paths={"Dashboard": str(tmp_path / "dashboard-checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(outbox_dir),
        subprocess_run=subprocess_run,
    )

    # Still within the limit window: no call is made at all.
    outcome_1 = run_tick(client, registry, run_fn, default_providers=["claude"], lock_manager=ProjectLockManager())
    assert outcome_1.ran is False
    assert calls == []

    # Past retry_after: resumes automatically, carrying the checkpoint
    # through into the spec file handed to ai-orchestrator.
    clock_holder["now"] += timedelta(minutes=31)
    outcome_2 = run_tick(client, registry, run_fn, default_providers=["claude"], lock_manager=ProjectLockManager())

    assert outcome_2.ran is True
    assert registry.get_status("claude").state == ProviderState.AVAILABLE
    spec = json.loads(open(_args_to_dict(calls[0])["spec"], encoding="utf-8").read())
    assert spec["checkpoint"] == {"step": 5}

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.checkpoint == {"step": 6}


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
        project_paths={"Dashboard": str(tmp_path / "dashboard-checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(tmp_path / "outbox"),
        subprocess_run=subprocess_run,
    )

    outcome = run_tick(client, registry, run_fn, default_providers=["claude"], lock_manager=ProjectLockManager())

    assert outcome.ran is True
    assert registry.get_status("claude").state == ProviderState.LIMITED

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status == ProjectStatus.IN_PROGRESS
    assert "session limit" in reloaded.stop_reason
