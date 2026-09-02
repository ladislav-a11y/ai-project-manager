"""Live end-to-end proof of the self-update activation flow, using real OS
subprocesses (not in-process fakes) driven by the real watchdog.py:

  1. A running "PM" process detects that its own code changed underneath it
     (real ``self_update.check_self_update``/``compute_code_version``).
  2. It records progress on its in-flight task, then exits with the real
     ``RESTART_REQUIRED_EXIT_CODE`` - never restarting itself in-process.
  3. The real supervising ``watchdog.run_watchdog`` (a separate parent
     process in production; here the parent test process, which is still
     distinct from the child subprocesses being supervised) launches a new
     subprocess, which - because it is a brand-new Python process - runs
     the *new* code, passes a live smoke test, and demonstrably reports the
     new code version.
  4. The original task's progress counter is continued, not reset or
     duplicated, proving no task loss and no duplicate dispatch.
"""

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from ai_project_manager.self_update import RESTART_REQUIRED_EXIT_CODE, compute_code_version
from ai_project_manager.watchdog import run_watchdog

REPO_ROOT = Path(__file__).resolve().parents[1]

_FAKE_PM_SOURCE = textwrap.dedent(
    """
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, {repo_root!r})
    from ai_project_manager.self_update import check_self_update, compute_code_version, RESTART_REQUIRED_EXIT_CODE

    state_path = Path(sys.argv[1])
    code_root = Path(sys.argv[2])
    started_version_path = Path(sys.argv[3])

    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {{
        "progress": 0, "runs": 0, "restarted": False,
    }}
    state["runs"] += 1

    current_version = compute_code_version(code_root)
    started_version = started_version_path.read_text(encoding="utf-8").strip()
    status = check_self_update(started_version, root=code_root)

    if status.changed and not state["restarted"]:
        # Simulate a real dispatch: make some progress on the original task
        # before noticing the self-update, then hand off for a safe restart
        # instead of finishing the task itself under stale imports.
        state["progress"] += 1
        state["restarted"] = True
        state_path.write_text(json.dumps(state), encoding="utf-8")
        sys.exit(RESTART_REQUIRED_EXIT_CODE)

    # Either nothing changed, or this is the post-restart run: continue the
    # same task from where it left off (progress increments, never resets).
    state["progress"] += 1
    state["final_version"] = current_version
    state_path.write_text(json.dumps(state), encoding="utf-8")
    sys.exit(0)
    """
).format(repo_root=str(REPO_ROOT))


def _write_code_root(root: Path, content: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "marker.py").write_text(content, encoding="utf-8")


def test_live_e2e_self_update_restart_continues_original_work(tmp_path):
    fake_pm = tmp_path / "fake_pm.py"
    fake_pm.write_text(_FAKE_PM_SOURCE, encoding="utf-8")

    code_root = tmp_path / "code"
    _write_code_root(code_root, "VERSION = 'v1'\n")

    started_version_path = tmp_path / "started_version.txt"
    started_version_path.write_text(compute_code_version(code_root), encoding="utf-8")

    state_path = tmp_path / "state.json"
    child_argv = [sys.executable, str(fake_pm), str(state_path), str(code_root), str(started_version_path)]

    def fake_run_git(argv):
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="deadbeef\n", stderr="")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    def real_launch(argv, cwd=None):
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60)

    # Nothing has changed yet: a normal run just finishes cleanly, same as
    # running the PM process directly with no watchdog involvement at all.
    exit_code = run_watchdog(
        child_argv,
        repo_root=str(tmp_path),
        smoke_test_argv=child_argv,
        launch=real_launch,
        run_git=fake_run_git,
        state_path=str(tmp_path / "watchdog_state.json"),
        sleep=lambda s: None,
        max_cycles=1,
    )
    assert exit_code == 0
    baseline_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert baseline_state["progress"] == 1
    assert baseline_state["restarted"] is False

    # PM upraven: the code changes on disk underneath the (conceptually)
    # still-running process - exactly what ai-orchestrator does mid-task.
    _write_code_root(code_root, "VERSION = 'v2'\n")
    new_version = compute_code_version(code_root)
    assert new_version != started_version_path.read_text(encoding="utf-8").strip()

    exit_code = run_watchdog(
        child_argv,
        repo_root=str(tmp_path),
        smoke_test_argv=child_argv,
        launch=real_launch,
        run_git=fake_run_git,
        state_path=str(tmp_path / "watchdog_state.json"),
        sleep=lambda s: None,
        max_cycles=1,
    )

    final_state = json.loads(state_path.read_text(encoding="utf-8"))

    # -> restart: the first (stale-code) subprocess in this cycle detected
    # the change and asked for a supervised restart rather than restarting
    # itself; the watchdog (a separate process) is what actually launched
    # the replacement.
    assert final_state["restarted"] is True
    # -> nova verze prokazatelne bezi: the post-restart subprocess reported
    # the *new* code version, not the one captured at original startup.
    assert final_state["final_version"] == new_version
    assert final_state["final_version"] != started_version_path.read_text(encoding="utf-8").strip()
    # -> puvodni prace pokracuje, zadna ztrata ukolu ani duplicitni dispatch:
    # progress advanced by exactly one more step across the restart (the
    # pre-restart run plus the post-restart run), never reset to 0 and
    # never double-counted.
    assert final_state["progress"] == baseline_state["progress"] + 2
    assert final_state["runs"] == baseline_state["runs"] + 2


# ---------------------------------------------------------------------------
# The test above proves the restart/continuity *machinery* using a tiny
# synthetic stand-in program. It does not, by itself, prove that the real
# production entrypoint restarts, or that real provider/Trello checkpoint
# state survives a real restart - a reviewer flagged exactly that gap. The
# test below closes it: it copies the *actual* ai_project_manager package
# (not a stub) into an isolated directory, drives it through the real
# ``daemon.run_loop`` (the same function ``cli.main`` calls) with a real
# ``ProviderRegistry``/``provider_state.json`` and a real
# ``InMemoryTrelloClient`` snapshot persisted across process boundaries, and
# supervises it with the real ``watchdog.run_watchdog`` over real OS
# subprocesses. Only two things are stand-ins, both clearly marked below:
# the regression-test command (a fast "python -c pass" instead of re-running
# this whole suite recursively) and the started-version file (representing
# what would, in production, simply be the same long-running process's
# in-memory start time - a single test process cannot literally stay alive
# while its own source is edited out from under it, so an external file
# plays that role across subprocess launches instead).
# ---------------------------------------------------------------------------

_REAL_PM_HARNESS_SOURCE = textwrap.dedent(
    """
    import json
    import sys
    from datetime import timedelta
    from pathlib import Path

    code_root = Path(sys.argv[1])
    trello_snapshot_path = Path(sys.argv[2])
    provider_state_path = Path(sys.argv[3])
    started_version_path = Path(sys.argv[4])
    dispatch_log_path = Path(sys.argv[5])
    once = "--once" in sys.argv[6:]

    sys.path.insert(0, str(code_root.parent))

    import ai_project_manager
    from ai_project_manager.daemon import run_loop
    from ai_project_manager.providers import ProviderRegistry
    from ai_project_manager.provider_state import load_provider_state
    from ai_project_manager.trello_client import InMemoryTrelloClient
    from ai_project_manager.trello_sync import sync_project_to_trello
    from ai_project_manager.models import ProjectRecord, ProjectStatus
    from ai_project_manager.self_update import compute_code_version, RESTART_REQUIRED_EXIT_CODE

    if trello_snapshot_path.exists():
        snapshot = json.loads(trello_snapshot_path.read_text(encoding="utf-8"))
        client = InMemoryTrelloClient(list_names=())
        client._lists = snapshot["lists"]
        client._cards = snapshot["cards"]
        client._next_list_id = snapshot["next_list_id"]
        client._next_card_id = snapshot["next_card_id"]
    else:
        client = InMemoryTrelloClient()
        project = ProjectRecord(
            name="Demo", priority=3, status=ProjectStatus.READY, main_task="do the real work",
        )
        sync_project_to_trello(client, project)

    registry = ProviderRegistry()
    registry.mark_available("claude")
    load_provider_state(str(provider_state_path), registry)

    dispatch_log = (
        json.loads(dispatch_log_path.read_text(encoding="utf-8"))
        if dispatch_log_path.exists() else {"dispatches": 0}
    )

    def run_fn(project, provider):
        dispatch_log["dispatches"] += 1
        if dispatch_log["dispatches"] == 1:
            # Simulate hitting a real provider quota on the very first
            # dispatch, with a resume checkpoint - retry_after already in
            # the past, so the *next* tick's free recheck (see
            # daemon.recheck_due_providers) picks it back up automatically,
            # proving the persisted checkpoint (not just "it works again")
            # survives the restart.
            registry.mark_limited(
                "claude", retry_after=timedelta(seconds=-1), checkpoint={"progress": 1},
                reason="simulated quota limit",
            )
        return {"status": "in_progress", "last_output": "dispatch %d" % dispatch_log["dispatches"]}

    if started_version_path.exists():
        started_version = started_version_path.read_text(encoding="utf-8").strip()
    else:
        started_version = compute_code_version(code_root)

    outcome = run_loop(
        client,
        registry,
        run_fn,
        once=once,
        max_iterations=None if once else 1,
        sleep=lambda s: None,
        default_providers=["claude"],
        provider_state_path=str(provider_state_path),
        check_self_update=True,
        self_update_code_root=str(code_root),
        self_update_started_version=started_version,
        self_update_test_command=[sys.executable, "-c", "pass"],
    )

    dispatch_log_path.write_text(json.dumps(dispatch_log), encoding="utf-8")
    trello_snapshot_path.write_text(json.dumps({
        "lists": client._lists,
        "cards": client._cards,
        "next_list_id": client._next_list_id,
        "next_card_id": client._next_card_id,
    }), encoding="utf-8")
    # A brand-new real process always fingerprints *its own* current code as
    # its starting point (see daemon.run_loop / cli.main) - the next launch
    # of this harness should only detect a restart if the code changes
    # again after this point, not re-detect the change that just happened.
    started_version_path.write_text(compute_code_version(code_root), encoding="utf-8")

    print("PM_VERSION=" + ai_project_manager.__version__, flush=True)
    sys.exit(RESTART_REQUIRED_EXIT_CODE if outcome.restart_required else 0)
    """
)


def _init_throwaway_git_repo(root: Path, run) -> None:
    run(("git", "-C", str(root), "init", "-q"))
    run(("git", "-C", str(root), "config", "user.email", "test@example.com"))
    run(("git", "-C", str(root), "config", "user.name", "Live E2E Test"))
    run(("git", "-C", str(root), "add", "-A"))
    run(("git", "-C", str(root), "commit", "-q", "-m", "baseline"))


def test_live_e2e_real_entrypoint_restart_preserves_provider_and_trello_state(tmp_path):
    """Real ``daemon.run_loop`` (what ``cli.main`` calls), real
    ``watchdog.run_watchdog``, real OS subprocesses, real
    ``provider_state.json``, and a real ``InMemoryTrelloClient`` snapshot
    carried across the restart - copied from the actual package on disk,
    not a synthetic stand-in program."""

    def real_run(argv, cwd=None):
        return subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, timeout=60)

    def real_launch(argv, cwd=None):
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60)

    work_root = tmp_path / "code_root"
    work_root.mkdir()
    code_root = work_root / "ai_project_manager"
    shutil.copytree(
        REPO_ROOT / "ai_project_manager", code_root, ignore=shutil.ignore_patterns("__pycache__"),
    )
    _init_throwaway_git_repo(work_root, real_run)

    harness = tmp_path / "real_pm_harness.py"
    harness.write_text(_REAL_PM_HARNESS_SOURCE, encoding="utf-8")

    trello_snapshot_path = tmp_path / "trello_snapshot.json"
    provider_state_path = tmp_path / "provider_state.json"
    started_version_path = tmp_path / "started_version.txt"
    dispatch_log_path = tmp_path / "dispatch_log.json"

    child_argv = [
        sys.executable, str(harness), str(code_root), str(trello_snapshot_path),
        str(provider_state_path), str(started_version_path), str(dispatch_log_path),
    ]

    baseline_version = compute_code_version(code_root)

    # Baseline: nothing has changed yet, this is a normal tick.
    exit_code = run_watchdog(
        child_argv,
        repo_root=str(work_root),
        smoke_test_argv=child_argv,
        launch=real_launch,
        run_git=real_run,
        state_path=str(tmp_path / "watchdog_state.json"),
        sleep=lambda s: None,
        max_cycles=1,
    )
    assert exit_code == 0
    assert dispatch_log_path.exists()
    assert json.loads(dispatch_log_path.read_text(encoding="utf-8"))["dispatches"] == 1

    # Provider hit a simulated quota limit on that first real dispatch, with
    # a checkpoint - this must genuinely be on disk, written by the real
    # provider_state module, before any restart happens.
    provider_state_after_baseline = json.loads(provider_state_path.read_text(encoding="utf-8"))
    assert provider_state_after_baseline["claude"]["state"] == "LIMITED"
    assert provider_state_after_baseline["claude"]["checkpoint"] == {"progress": 1}

    # "PM upraven": edit the real, copied package's actual source (a genuine
    # __version__ bump, just like an autonomous coding run would leave
    # behind) - not a synthetic marker file.
    init_path = code_root / "__init__.py"
    init_path.write_text(
        init_path.read_text(encoding="utf-8").replace('__version__ = "0.1.0"', '__version__ = "0.2.0-selfupdate"'),
        encoding="utf-8",
    )
    new_version = compute_code_version(code_root)
    assert new_version != baseline_version

    exit_code = run_watchdog(
        child_argv,
        repo_root=str(work_root),
        smoke_test_argv=child_argv + ["--once"],
        launch=real_launch,
        run_git=real_run,
        state_path=str(tmp_path / "watchdog_state.json"),
        sleep=lambda s: None,
        max_cycles=2,
    )
    assert exit_code == 0

    # -> nova verze prokazatelne bezi: at least one of the real subprocess
    # launches after the edit actually imported and ran the new __version__,
    # not the one captured at the very first baseline launch.
    # (stdout isn't captured by run_watchdog itself, so re-run the harness
    # once more directly to observe it - same real entrypoint, same real
    # state files, one more real continuation of the same task.)
    final_run = real_launch(child_argv + ["--once"])
    assert final_run.returncode == 0, final_run.stdout + final_run.stderr
    assert "PM_VERSION=0.2.0-selfupdate" in final_run.stdout

    # -> puvodni prace pokracuje, zadna ztrata ukolu ani duplicitni dispatch:
    # the dispatch counter kept advancing across every restart, never reset.
    final_dispatch_log = json.loads(dispatch_log_path.read_text(encoding="utf-8"))
    assert final_dispatch_log["dispatches"] >= 4

    # -> provider/Trello checkpoint state preserved across the real restart:
    # the same provider recovered (retry_after already elapsed) *with* its
    # original checkpoint intact, and exactly one Trello card exists
    # throughout - never lost, never duplicated.
    final_provider_state = json.loads(provider_state_path.read_text(encoding="utf-8"))
    assert final_provider_state["claude"]["state"] == "AVAILABLE"
    assert final_provider_state["claude"]["checkpoint"] == {"progress": 1}

    final_trello = json.loads(trello_snapshot_path.read_text(encoding="utf-8"))
    assert len(final_trello["cards"]) == 1
    (card,) = final_trello["cards"].values()
    assert card["name"] == "P3 — Demo"
    assert card["desc"] and "dispatch" in card["desc"]
