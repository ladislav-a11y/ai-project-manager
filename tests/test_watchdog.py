import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_project_manager.self_update import RESTART_REQUIRED_EXIT_CODE
from ai_project_manager.watchdog import (
    DEFAULT_MAX_CONSECUTIVE_RESTARTS,
    DEFAULT_RESTART_BACKOFF_SECONDS,
    WatchdogAlreadyRunning,
    WatchdogProcessLock,
    WatchdogState,
    _resolve_python_executable,
    build_parser,
    main,
    run_watchdog,
)


def _result(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _fake_git(head="abc123"):
    def run_git(argv):
        if "rev-parse" in argv:
            return _result(0, stdout=f"{head}\n")
        if "worktree" in argv:
            return _result(0)
        raise AssertionError(f"unexpected git call: {argv}")
    return run_git


def test_process_lock_refuses_second_watchdog_and_is_reusable_after_release(tmp_path):
    lock_path = tmp_path / "runtime" / "watchdog.lock"

    with WatchdogProcessLock(lock_path):
        assert lock_path.is_file()
        with pytest.raises(WatchdogAlreadyRunning, match="already owns"):
            with WatchdogProcessLock(lock_path):
                pass

    # Releasing the OS lock, not deleting the marker file, makes a later
    # legitimate restart safe after either a clean exit or process crash.
    with WatchdogProcessLock(lock_path):
        assert lock_path.is_file()


def test_normal_exit_stops_watchdog_without_restart(tmp_path):
    launches = []

    def launch(argv, cwd=None):
        launches.append((list(argv), cwd))
        return _result(0)

    exit_code = run_watchdog(
        ["child"],
        repo_root=str(tmp_path),
        launch=launch,
        run_git=_fake_git(),
        state_path=str(tmp_path / "state.json"),
        sleep=lambda s: None,
    )

    assert exit_code == 0
    assert len(launches) == 1
    assert launches[0] == (["child"], str(tmp_path))


def test_restart_required_relaunches_and_records_success(tmp_path):
    launches = []

    def launch(argv, cwd=None):
        launches.append((list(argv), cwd))
        if argv == ["child"] and len(launches) == 1:
            return _result(RESTART_REQUIRED_EXIT_CODE)
        if argv == ["smoke"]:
            return _result(0)
        return _result(0)

    exit_code = run_watchdog(
        ["child"],
        repo_root=str(tmp_path),
        smoke_test_argv=["smoke"],
        launch=launch,
        run_git=_fake_git(),
        state_path=str(tmp_path / "state.json"),
        sleep=lambda s: None,
        max_cycles=1,
    )

    # cycle 1: child requests restart, smoke test passes, max_cycles=1 stops
    # the loop right after - the important assertions are the sequence and
    # that the watchdog is what launched both processes, not the child.
    assert launches[0] == (["child"], str(tmp_path))
    assert (["smoke"], str(tmp_path)) in launches


def test_failed_smoke_test_triggers_nondestructive_rollback(tmp_path):
    launches = []
    git_calls = []

    def launch(argv, cwd=None):
        launches.append((list(argv), cwd))
        if argv == ["child"] and cwd == str(tmp_path):
            return _result(RESTART_REQUIRED_EXIT_CODE)
        if argv == ["smoke"]:
            return _result(1, stdout="smoke test failed")
        return _result(0)

    def run_git(argv):
        git_calls.append(argv)
        if "rev-parse" in argv:
            return _result(0, stdout="goodcommit\n")
        if "worktree" in argv and "add" in argv:
            return _result(0)
        if "worktree" in argv and "remove" in argv:
            return _result(0)
        raise AssertionError(argv)

    exit_code = run_watchdog(
        ["child"],
        repo_root=str(tmp_path),
        smoke_test_argv=["smoke"],
        launch=launch,
        run_git=run_git,
        state_path=str(tmp_path / "state.json"),
        sleep=lambda s: None,
        max_cycles=2,
    )

    # No destructive Git operation (checkout --force / reset) is ever used
    # for rollback - only a dedicated, isolated worktree.
    assert not any("checkout" in c for c in git_calls)
    assert not any("reset" in c for c in git_calls)
    add_calls = [c for c in git_calls if "worktree" in c and "add" in c]
    assert len(add_calls) == 1
    assert "goodcommit" in add_calls[0]
    remove_calls = [c for c in git_calls if "worktree" in c and "remove" in c]
    assert len(remove_calls) == 1
    assert "--force" in remove_calls[0]

    expected_rollback_dir = str(tmp_path / "runtime" / "self_update_rollback_worktree")
    # After the failed smoke test, the child is relaunched from the
    # non-destructive rollback worktree, not from repo_root.
    rollback_launches = [cwd for argv, cwd in launches if argv == ["child"] and cwd == expected_rollback_dir]
    assert rollback_launches, launches


def test_rollback_worktree_failure_leaves_primary_tree_untouched(tmp_path):
    """If even the non-destructive rollback itself cannot be prepared (e.g.
    a locked worktree directory), the watchdog must not fall back to any
    destructive Git operation - it just keeps retrying the primary tree."""
    launches = []
    git_calls = []

    def launch(argv, cwd=None):
        launches.append((list(argv), cwd))
        if argv == ["child"]:
            return _result(RESTART_REQUIRED_EXIT_CODE)
        if argv == ["smoke"]:
            return _result(1, stdout="smoke test failed")
        return _result(0)

    def run_git(argv):
        git_calls.append(argv)
        if "rev-parse" in argv:
            return _result(0, stdout="goodcommit\n")
        if "worktree" in argv and "remove" in argv:
            return _result(1, stderr="not a working tree")
        if "worktree" in argv and "add" in argv:
            return _result(128, stderr="fatal: could not create worktree")
        raise AssertionError(argv)

    exit_code = run_watchdog(
        ["child"],
        repo_root=str(tmp_path),
        smoke_test_argv=["smoke"],
        launch=launch,
        run_git=run_git,
        state_path=str(tmp_path / "state.json"),
        sleep=lambda s: None,
        max_cycles=2,
    )

    assert not any("checkout" in c for c in git_calls)
    assert not any("reset" in c for c in git_calls)
    # Every relaunch of "child" still targets the primary tree -
    # never a half-prepared or missing rollback directory.
    child_launches = [cwd for argv, cwd in launches if argv == ["child"]]
    assert all(cwd == str(tmp_path) for cwd in child_launches)


def test_failed_rollback_refresh_does_not_relaunch_from_removed_worktree(tmp_path):
    """A previously active rollback path becomes invalid when refreshing the
    worktree removes it but the following add fails. The watchdog must clear
    that stale cwd before its next child launch."""
    launches = []
    add_attempts = 0

    def launch(argv, cwd=None):
        launches.append((list(argv), cwd))
        if argv == ["smoke"]:
            return _result(1, stdout="candidate is still broken")
        return _result(RESTART_REQUIRED_EXIT_CODE)

    def run_git(argv):
        nonlocal add_attempts
        if "rev-parse" in argv:
            return _result(0, stdout="goodcommit\n")
        if "worktree" in argv and "remove" in argv:
            return _result(0)
        if "worktree" in argv and "add" in argv:
            add_attempts += 1
            return _result(0 if add_attempts == 1 else 128, stderr="could not recreate worktree")
        raise AssertionError(argv)

    run_watchdog(
        ["child"],
        repo_root=str(tmp_path),
        smoke_test_argv=["smoke"],
        launch=launch,
        run_git=run_git,
        state_path=str(tmp_path / "state.json"),
        sleep=lambda s: None,
        max_cycles=3,
    )

    rollback_dir = str(tmp_path / "runtime" / "self_update_rollback_worktree")
    child_cwds = [cwd for argv, cwd in launches if argv == ["child"]]
    assert child_cwds == [str(tmp_path), rollback_dir, str(tmp_path)]


def test_dirty_previous_rollback_worktree_is_replaced_before_fallback(tmp_path):
    """Runtime files from an earlier fallback must not prevent a later
    failed candidate from restoring the known-good worktree again."""
    git_calls = []

    def run_git(argv):
        git_calls.append(argv)
        if "rev-parse" in argv:
            return _result(0, stdout="goodcommit\n")
        if "worktree" in argv and "remove" in argv:
            return _result(0 if "--force" in argv else 128, stderr="contains modified files")
        if "worktree" in argv and "add" in argv:
            return _result(0)
        raise AssertionError(argv)

    launches = []

    def launch(argv, cwd=None):
        launches.append((list(argv), cwd))
        if argv == ["smoke"]:
            return _result(1)
        if cwd == str(tmp_path):
            return _result(RESTART_REQUIRED_EXIT_CODE)
        return _result(0)

    run_watchdog(
        ["child"],
        repo_root=str(tmp_path),
        smoke_test_argv=["smoke"],
        launch=launch,
        run_git=run_git,
        state_path=str(tmp_path / "state.json"),
        sleep=lambda s: None,
        max_cycles=2,
    )

    expected = str(tmp_path / "runtime" / "self_update_rollback_worktree")
    assert (["child"], expected) in launches
    assert any("remove" in call and "--force" in call for call in git_calls)


def test_exceeds_max_consecutive_restarts_stays_down(tmp_path):
    def launch(argv, cwd=None):
        return _result(RESTART_REQUIRED_EXIT_CODE)

    exit_code = run_watchdog(
        ["child"],
        repo_root=str(tmp_path),
        smoke_test_argv=None,
        launch=launch,
        run_git=_fake_git(),
        state_path=str(tmp_path / "state.json"),
        sleep=lambda s: None,
        max_consecutive_restarts=2,
    )

    assert exit_code == RESTART_REQUIRED_EXIT_CODE


def test_repeated_failed_smoke_tests_do_not_reset_restart_limit(tmp_path):
    launches = []

    def launch(argv, cwd=None):
        launches.append((list(argv), cwd))
        if argv == ["smoke"]:
            return _result(1, stdout="candidate is still broken")
        return _result(RESTART_REQUIRED_EXIT_CODE)

    exit_code = run_watchdog(
        ["child"],
        repo_root=str(tmp_path),
        smoke_test_argv=["smoke"],
        launch=launch,
        run_git=_fake_git(),
        state_path=str(tmp_path / "state.json"),
        sleep=lambda s: None,
        max_consecutive_restarts=2,
    )

    assert exit_code == RESTART_REQUIRED_EXIT_CODE
    assert len([argv for argv, _cwd in launches if argv == ["child"]]) == 3
    assert len([argv for argv, _cwd in launches if argv == ["smoke"]]) == 2


def test_watchdog_state_round_trips(tmp_path):
    path = tmp_path / "state.json"
    state = WatchdogState(last_known_good_commit="abc123")
    state.save(path)

    loaded = WatchdogState.load(path)

    assert loaded.last_known_good_commit == "abc123"


def test_watchdog_state_save_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "state.json"

    WatchdogState(last_known_good_commit="abc123").save(path)

    assert list(tmp_path.iterdir()) == [path]


def test_watchdog_state_save_failure_preserves_old_state_and_cleans_temp(tmp_path):
    path = tmp_path / "state.json"
    WatchdogState(last_known_good_commit="old").save(path)

    with patch("ai_project_manager.watchdog.os.replace", side_effect=PermissionError("locked")):
        with pytest.raises(PermissionError, match="locked"):
            WatchdogState(last_known_good_commit="new").save(path)

    assert WatchdogState.load(path).last_known_good_commit == "old"
    assert list(tmp_path.iterdir()) == [path]


def test_watchdog_state_load_missing_file_returns_default(tmp_path):
    loaded = WatchdogState.load(tmp_path / "missing.json")

    assert loaded.last_known_good_commit is None


def test_watchdog_state_load_corrupt_file_returns_default(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not json{{{", encoding="utf-8")

    loaded = WatchdogState.load(path)

    assert loaded.last_known_good_commit is None


def test_build_parser_forwards_child_args():
    args = build_parser().parse_args(["--repo-root", "R", "--log-level", "DEBUG", "--", "--once"])

    assert args.repo_root == "R"
    assert "--once" in args.child_args


def test_build_parser_exposes_restart_guard_configuration():
    args = build_parser().parse_args(
        ["--max-consecutive-restarts", "2", "--restart-backoff-seconds", "0.25"]
    )

    assert args.max_consecutive_restarts == 2
    assert args.restart_backoff_seconds == 0.25


def test_build_parser_restart_guard_defaults_match_runtime_defaults():
    args = build_parser().parse_args([])

    assert args.max_consecutive_restarts == DEFAULT_MAX_CONSECUTIVE_RESTARTS
    assert args.restart_backoff_seconds == DEFAULT_RESTART_BACKOFF_SECONDS


@pytest.mark.parametrize(
    "option,value",
    [
        ("--max-consecutive-restarts", "-1"),
        ("--restart-backoff-seconds", "-0.1"),
    ],
)
def test_main_rejects_negative_restart_guard_values(option, value):
    with pytest.raises(SystemExit) as exc_info:
        main([option, value])

    assert exc_info.value.code == 2


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_main_rejects_non_finite_restart_backoff(value):
    """A non-finite float passes an ordinary negative-number check; NaN
    would then reach ``time.sleep`` and crash the supervisor during recovery."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--restart-backoff-seconds", value])

    assert exc_info.value.code == 2


def test_main_passes_restart_guard_configuration_to_watchdog(tmp_path):
    with patch("ai_project_manager.watchdog.run_watchdog", return_value=0) as run:
        exit_code = main(
            [
                "--repo-root", str(tmp_path),
                "--max-consecutive-restarts", "3",
                "--restart-backoff-seconds", "0.5",
                "--no-smoke-test",
            ]
        )

    assert exit_code == 0
    assert run.call_args.kwargs["max_consecutive_restarts"] == 3
    assert run.call_args.kwargs["restart_backoff_seconds"] == 0.5


def test_main_returns_failure_without_launching_child_when_watchdog_is_already_running(tmp_path):
    with WatchdogProcessLock(tmp_path / "runtime" / "watchdog.lock"):
        with patch("ai_project_manager.watchdog.run_watchdog") as run:
            exit_code = main(["--repo-root", str(tmp_path), "--no-smoke-test"])

    assert exit_code == 1
    run.assert_not_called()


def test_repository_relative_python_is_stable_across_rollback_cwd(tmp_path):
    resolved = _resolve_python_executable(
        str(Path(".venv") / "Scripts" / "python.exe"),
        str(tmp_path),
    )

    assert resolved == str((tmp_path / ".venv" / "Scripts" / "python.exe").resolve())


def test_bare_python_command_keeps_path_lookup(tmp_path):
    assert _resolve_python_executable("python", str(tmp_path)) == "python"
