import subprocess

import pytest

from ai_project_manager.self_update import (
    RESTART_REQUIRED_EXIT_CODE,
    check_self_update,
    compute_code_version,
    prepare_safe_restart,
    run_regression_tests,
    verify_git_checkpoint,
)


def _write(path, text):
    path.write_text(text, encoding="utf-8")


def test_compute_code_version_stable_when_unchanged(tmp_path):
    _write(tmp_path / "a.py", "x = 1\n")
    _write(tmp_path / "b.py", "y = 2\n")

    assert compute_code_version(tmp_path) == compute_code_version(tmp_path)


def test_compute_code_version_changes_on_content_edit(tmp_path):
    _write(tmp_path / "a.py", "x = 1\n")
    before = compute_code_version(tmp_path)

    _write(tmp_path / "a.py", "x = 2\n")
    after = compute_code_version(tmp_path)

    assert before != after


def test_compute_code_version_unaffected_by_mtime_only_touch(tmp_path):
    path = tmp_path / "a.py"
    _write(path, "x = 1\n")
    before = compute_code_version(tmp_path)

    # Rewrite identical content - a checkout can reset mtimes without any
    # real edit; the fingerprint must not treat that as a self-update.
    _write(path, "x = 1\n")
    after = compute_code_version(tmp_path)

    assert before == after


def test_compute_code_version_ignores_pycache(tmp_path):
    _write(tmp_path / "a.py", "x = 1\n")
    before = compute_code_version(tmp_path)

    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    _write(pycache / "a.cpython-312.pyc", "garbage")

    assert compute_code_version(tmp_path) == before


def test_compute_code_version_detects_new_file(tmp_path):
    _write(tmp_path / "a.py", "x = 1\n")
    before = compute_code_version(tmp_path)

    _write(tmp_path / "c.py", "z = 3\n")

    assert compute_code_version(tmp_path) != before


def test_check_self_update_reports_unchanged(tmp_path):
    _write(tmp_path / "a.py", "x = 1\n")
    started = compute_code_version(tmp_path)

    status = check_self_update(started, root=tmp_path)

    assert status.changed is False
    assert status.started_version == started
    assert status.current_version == started


def test_check_self_update_reports_changed(tmp_path):
    _write(tmp_path / "a.py", "x = 1\n")
    started = compute_code_version(tmp_path)

    _write(tmp_path / "a.py", "x = 999\n")

    status = check_self_update(started, root=tmp_path)

    assert status.changed is True
    assert status.current_version != started


def _fake_result(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_verify_git_checkpoint_ok_on_clean_resolvable_head(tmp_path):
    calls = []

    def run_git(argv):
        calls.append(argv)
        if "rev-parse" in argv:
            return _fake_result(0, stdout="abc123\n")
        if "status" in argv:
            return _fake_result(0, stdout="")
        raise AssertionError(f"unexpected git call: {argv}")

    ok, reason, details = verify_git_checkpoint(str(tmp_path), run_git=run_git)

    assert ok is True
    assert details["commit"] == "abc123"


def test_verify_git_checkpoint_ok_with_uncommitted_changes():
    """Uncommitted edits are the normal state before a restart - this
    project's own convention is that only the orchestrator commits, after
    tests pass - so a dirty-but-unconflicted tree must still verify."""

    def run_git(argv):
        if "rev-parse" in argv:
            return _fake_result(0, stdout="abc123\n")
        if "status" in argv:
            return _fake_result(0, stdout=" M ai_project_manager/daemon.py\n?? new_file.py\n")
        raise AssertionError(f"unexpected git call: {argv}")

    ok, reason, details = verify_git_checkpoint("repo", run_git=run_git)

    assert ok is True


def test_verify_git_checkpoint_fails_on_unresolvable_head():
    def run_git(argv):
        if "rev-parse" in argv:
            return _fake_result(128, stderr="fatal: not a git repository")
        raise AssertionError(f"unexpected git call: {argv}")

    ok, reason, details = verify_git_checkpoint("repo", run_git=run_git)

    assert ok is False
    assert "not resolvable" in reason


def test_verify_git_checkpoint_fails_on_merge_conflict():
    def run_git(argv):
        if "rev-parse" in argv:
            return _fake_result(0, stdout="abc123\n")
        if "status" in argv:
            return _fake_result(0, stdout="UU ai_project_manager/daemon.py\n")
        raise AssertionError(f"unexpected git call: {argv}")

    ok, reason, details = verify_git_checkpoint("repo", run_git=run_git)

    assert ok is False
    assert "conflict" in reason


def test_run_regression_tests_reports_pass():
    def run(command, cwd):
        return _fake_result(0, stdout="5 passed")

    ok, reason = run_regression_tests("repo", run=run)

    assert ok is True


def test_run_regression_tests_reports_failure_with_tail():
    def run(command, cwd):
        return _fake_result(1, stdout="FAILED tests/test_x.py::test_y")

    ok, reason = run_regression_tests("repo", run=run)

    assert ok is False
    assert "FAILED" in reason


def test_prepare_safe_restart_persists_state_only_when_safe(tmp_path):
    persisted = []

    def run_tests(command, cwd):
        return _fake_result(0)

    def run_git(argv):
        if "rev-parse" in argv:
            return _fake_result(0, stdout="abc123\n")
        if "status" in argv:
            return _fake_result(0, stdout="")
        raise AssertionError(argv)

    readiness = prepare_safe_restart(
        str(tmp_path),
        persist_state=lambda: persisted.append(True),
        run_tests=run_tests,
        run_git=run_git,
    )

    assert readiness.safe is True
    assert persisted == [True]


def test_prepare_safe_restart_never_persists_state_when_tests_fail(tmp_path):
    persisted = []

    def run_tests(command, cwd):
        return _fake_result(1, stdout="FAILED")

    def run_git(argv):
        raise AssertionError("git must not be consulted when tests already failed")

    readiness = prepare_safe_restart(
        str(tmp_path),
        persist_state=lambda: persisted.append(True),
        run_tests=run_tests,
        run_git=run_git,
    )

    assert readiness.safe is False
    assert readiness.tests_passed is False
    assert persisted == []


def test_prepare_safe_restart_never_persists_state_when_git_checkpoint_bad(tmp_path):
    persisted = []

    def run_tests(command, cwd):
        return _fake_result(0)

    def run_git(argv):
        if "rev-parse" in argv:
            return _fake_result(0, stdout="abc123\n")
        if "status" in argv:
            return _fake_result(0, stdout="UU conflicted.py\n")
        raise AssertionError(argv)

    readiness = prepare_safe_restart(
        str(tmp_path),
        persist_state=lambda: persisted.append(True),
        run_tests=run_tests,
        run_git=run_git,
    )

    assert readiness.safe is False
    assert readiness.tests_passed is True
    assert readiness.git_checkpoint_ok is False
    assert persisted == []


def test_prepare_safe_restart_defers_when_state_checkpoint_fails(tmp_path):
    def run_tests(command, cwd):
        return _fake_result(0)

    def run_git(argv):
        if "rev-parse" in argv:
            return _fake_result(0, stdout="abc123\n")
        if "status" in argv:
            return _fake_result(0, stdout="")
        raise AssertionError(argv)

    def persist_state():
        raise OSError("disk full")

    readiness = prepare_safe_restart(
        str(tmp_path),
        persist_state=persist_state,
        run_tests=run_tests,
        run_git=run_git,
    )

    assert readiness.safe is False
    assert readiness.tests_passed is True
    assert readiness.git_checkpoint_ok is True
    assert readiness.details == {"commit": "abc123"}
    assert "checkpoint failed" in readiness.reason
    assert "disk full" in readiness.reason


def test_restart_required_exit_code_is_distinct():
    assert RESTART_REQUIRED_EXIT_CODE not in (0, 1, 2)
