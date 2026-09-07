"""Live-contract end-to-end test for unattended blocked-task recovery:
review -> auto-repair/unblock -> requeue -> work actually continues,
exercised through the *real* handoff contract to ai-orchestrator (the
same ``--project``/``--goal``/``--spec``/``--agent``/``--run-id`` argv
shape and outbox result read-back as ``test_handoff_e2e.py``), not just
the in-memory ``recovery`` unit.

A blocked Trello card is never picked by the normal priority scheduler
(scheduler.is_schedulable excludes it) - the recovery pass in
``daemon.run_tick`` is the only thing that can ever bring it back. This
proves that pass actually drives a real dispatch end to end, in the same
tick, preserving the card's priority and in-flight checkpoint; and that a
genuinely unrecoverable block is left BLOCKED with a concrete, actionable
reason and never dispatched at all.
"""

import json
import subprocess

from ai_project_manager.daemon import run_tick
from ai_project_manager.lock import ProjectLockManager
from ai_project_manager.models import ProjectRecord as _ProjectRecord, ProjectStatus
from ai_project_manager.orchestrator_runner import build_run_fn, parse_spec_markdown
from ai_project_manager.providers import ProviderRegistry
from ai_project_manager.trello_client import InMemoryTrelloClient
from ai_project_manager.trello_sync import build_list_maps, project_from_card, sync_project_to_trello


def ProjectRecord(*args, **kwargs):
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


def test_blocked_card_is_reviewed_repaired_requeued_and_work_continues_same_tick(tmp_path):
    project = ProjectRecord(
        name="Dashboard",
        priority=4,
        status=ProjectStatus.BLOCKED,
        orchestrator_ready_task="Implement the live status widget",
        checkpoint={"step": 5},
        # A transient provider/protocol failure recorded by a previous
        # run - exactly the class of block that should be safely
        # auto-recoverable without any human involvement.
        blocked_by="connection reset while calling the provider",
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
                "checkpoint": {"step": 6},
                "last_output": "resumed after recovery",
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

    outcome = run_tick(
        client, registry, run_fn, default_providers=["claude"],
        lock_manager=ProjectLockManager(), provider_state_path=str(tmp_path / "provider_state.json"),
    )

    # review -> repair/unblock -> requeue -> work continues, all in the
    # one tick: the recovery pass ran before the scheduler even looked at
    # this project, and the same tick then actually dispatched it.
    assert outcome.ran is True
    assert len(calls) == 1
    args = _args_to_dict(calls[0])
    assert args["goal"] == "Implement the live status widget"
    # The checkpoint carried through the block into the real dispatch
    # untouched - recovery must never lose in-flight progress.
    spec = parse_spec_markdown(open(args["spec"], encoding="utf-8").read())
    assert spec["checkpoint"] == {"step": 5}

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)

    assert reloaded.blocked_by is None
    assert reloaded.status == ProjectStatus.IN_PROGRESS
    assert reloaded.priority == 4
    assert reloaded.checkpoint == {"step": 6}
    assert reloaded.last_output == "resumed after recovery"



def test_tool_call_validation_block_is_recovered_and_dispatched_same_tick(tmp_path):
    project = ProjectRecord(
        name="Dashboard",
        priority=3,
        status=ProjectStatus.BLOCKED,
        orchestrator_ready_task="Implement the live status widget",
        checkpoint={"run_id": "stale-run", "completed_dod_indices": []},
        blocked_by=(
            "Error code: 400 - {'error': {'message': "
            "\"Tool call validation failed: attempted to call tool 'commentary' "
            "which was not in request.tools\", 'code': 'tool_use_failed'}}"
        ),
    )
    client = InMemoryTrelloClient()
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    registry = ProviderRegistry()
    registry.mark_available("groq")

    outbox_dir = tmp_path / "outbox"
    subprocess_run, calls = _fake_ai_orchestrator(
        outbox_dir,
        responses={
            "Dashboard": {
                "checkpoint": {"run_id": "new-run", "completed_dod_indices": [0]},
                "last_output": "continued after AO tool-schema fix",
                "next_step": "audit",
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

    outcome = run_tick(
        client,
        registry,
        run_fn,
        default_providers=["groq"],
        lock_manager=ProjectLockManager(),
        provider_state_path=str(tmp_path / "provider_state.json"),
    )

    assert outcome.ran is True
    assert len(calls) == 1
    spec = parse_spec_markdown(open(_args_to_dict(calls[0])["spec"], encoding="utf-8").read())
    assert spec["checkpoint"] == {"run_id": "stale-run", "completed_dod_indices": []}

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.blocked_by is None
    assert reloaded.status == ProjectStatus.IN_PROGRESS
    assert reloaded.checkpoint == {"run_id": "new-run", "completed_dod_indices": [0]}


def test_blocked_card_needing_a_human_is_never_dispatched_to_the_real_orchestrator(tmp_path):
    project = ProjectRecord(
        name="Dashboard",
        priority=4,
        status=ProjectStatus.BLOCKED,
        orchestrator_ready_task="Implement the live status widget",
        checkpoint={"step": 5},
        blocked_by="missing deploy credentials - needs a human to provision access",
    )
    client = InMemoryTrelloClient()
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    registry = ProviderRegistry()
    registry.mark_available("claude")

    outbox_dir = tmp_path / "outbox"
    subprocess_run, calls = _fake_ai_orchestrator(outbox_dir, responses={})

    run_fn = build_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={"Dashboard": _checkout(tmp_path, "dashboard-checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(outbox_dir),
        subprocess_run=subprocess_run,
    )

    outcome = run_tick(
        client, registry, run_fn, default_providers=["claude"],
        lock_manager=ProjectLockManager(), provider_state_path=str(tmp_path / "provider_state.json"),
    )

    assert outcome.ran is False
    assert calls == []  # the real orchestrator process is never invoked

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status == ProjectStatus.BLOCKED
    assert "credential" in reloaded.blocked_by.lower()
    # In-flight progress is preserved even while parked for a human.
    assert reloaded.checkpoint == {"step": 5}
    assert reloaded.priority == 4
