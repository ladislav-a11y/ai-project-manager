"""Regression tests for self-update detection/restart wiring in
daemon.run_loop - see self_update.py for the underlying primitives and
watchdog.py for the separate supervising process that actually restarts."""

import subprocess

import pytest

from ai_project_manager.daemon import run_loop
from ai_project_manager.models import ProjectRecord, ProjectStatus
from ai_project_manager.providers import ProviderRegistry
from ai_project_manager.self_update import compute_code_version
from ai_project_manager.trello_client import InMemoryTrelloClient
from ai_project_manager.trello_sync import sync_project_to_trello


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _fake_result(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _idle_client():
    # No projects, no Inbox activity -> every tick is a fast, AI-free no-op,
    # so these tests only exercise the self-update wiring itself.
    return InMemoryTrelloClient()


def _noop_run_fn(project, provider):
    return {}


def _no_sleep(seconds):
    pass


def test_no_restart_requested_when_code_is_unchanged(tmp_path):
    code_root = tmp_path / "code"
    code_root.mkdir()
    (code_root / "a.py").write_text("x = 1\n", encoding="utf-8")
    started_version = compute_code_version(code_root)

    client = _idle_client()
    registry = ProviderRegistry()

    outcome = run_loop(
        client,
        registry,
        _noop_run_fn,
        once=False,
        max_iterations=2,
        sleep=_no_sleep,
        check_self_update=True,
        self_update_code_root=str(code_root),
        self_update_started_version=started_version,
    )

    assert outcome.restart_required is False


def test_restart_requested_and_loop_exits_immediately_when_safe(tmp_path):
    code_root = tmp_path / "code"
    code_root.mkdir()
    (code_root / "a.py").write_text("x = 1\n", encoding="utf-8")
    started_version = compute_code_version(code_root)
    # Simulate the self-update itself: the file on disk changes underneath
    # the (conceptually) already-running process.
    (code_root / "a.py").write_text("x = 2\n", encoding="utf-8")

    test_calls = []

    def fake_run_tests(command, cwd):
        test_calls.append(cwd)
        return _fake_result(0)

    def fake_run_git(argv):
        if "rev-parse" in argv:
            return _fake_result(0, stdout="deadbeef\n")
        if "status" in argv:
            return _fake_result(0, stdout="")
        raise AssertionError(argv)

    client = _idle_client()
    registry = ProviderRegistry()

    outcome = run_loop(
        client,
        registry,
        _noop_run_fn,
        once=False,
        max_iterations=5,
        sleep=_no_sleep,
        check_self_update=True,
        self_update_code_root=str(code_root),
        self_update_started_version=started_version,
        self_update_run_tests=fake_run_tests,
        self_update_run_git=fake_run_git,
        provider_state_path=str(tmp_path / "provider_state.json"),
    )

    assert outcome.restart_required is True
    assert "self-update" in outcome.reason
    # The loop must return on the very first iteration once a safe restart
    # is verified - never grinding through the remaining max_iterations.
    assert len(test_calls) == 1
    assert (tmp_path / "provider_state.json").exists()


def test_restart_deferred_and_no_work_lost_when_tests_fail(tmp_path):
    code_root = tmp_path / "code"
    code_root.mkdir()
    (code_root / "a.py").write_text("x = 1\n", encoding="utf-8")
    started_version = compute_code_version(code_root)
    (code_root / "a.py").write_text("x = 2\n", encoding="utf-8")

    test_calls = []

    def fake_run_tests(command, cwd):
        test_calls.append(cwd)
        return _fake_result(1, stdout="FAILED tests/test_x.py")

    def fake_run_git(argv):
        raise AssertionError("git must not be consulted when tests already failed")

    project = ProjectRecord(name="Demo", priority=3, status=ProjectStatus.READY, main_task="do work")
    client = InMemoryTrelloClient()
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    registry = ProviderRegistry()
    registry.mark_available("claude")

    calls = []

    def run_fn(project, provider):
        calls.append(project.name)
        return {"status": "in_progress", "last_output": "worked"}

    outcome = run_loop(
        client,
        registry,
        run_fn,
        once=False,
        max_iterations=2,
        default_providers=["claude"],
        sleep=_no_sleep,
        check_self_update=True,
        self_update_code_root=str(code_root),
        self_update_started_version=started_version,
        self_update_run_tests=fake_run_tests,
        self_update_run_git=fake_run_git,
    )

    assert outcome.restart_required is False
    # The self-update check ran on every iteration (tests kept failing)...
    assert len(test_calls) == 2
    # ...but real scheduler work was never skipped because of it.
    assert calls == ["Demo"]


def test_once_mode_never_checks_self_update(tmp_path):
    code_root = tmp_path / "code"
    code_root.mkdir()
    (code_root / "a.py").write_text("x = 1\n", encoding="utf-8")
    started_version = compute_code_version(code_root)
    (code_root / "a.py").write_text("x = 2\n", encoding="utf-8")

    test_calls = []

    def fake_run_tests(command, cwd):
        test_calls.append(cwd)
        return _fake_result(0)

    client = _idle_client()
    registry = ProviderRegistry()

    outcome = run_loop(
        client,
        registry,
        _noop_run_fn,
        once=True,
        check_self_update=True,
        self_update_code_root=str(code_root),
        self_update_started_version=started_version,
        self_update_run_tests=fake_run_tests,
    )

    assert outcome.restart_required is False
    assert test_calls == []


def test_check_self_update_false_disables_the_whole_mechanism(tmp_path):
    code_root = tmp_path / "code"
    code_root.mkdir()
    (code_root / "a.py").write_text("x = 1\n", encoding="utf-8")
    started_version = compute_code_version(code_root)
    (code_root / "a.py").write_text("x = 2\n", encoding="utf-8")

    def fake_run_tests(command, cwd):
        raise AssertionError("must not run when check_self_update=False")

    client = _idle_client()
    registry = ProviderRegistry()

    outcome = run_loop(
        client,
        registry,
        _noop_run_fn,
        once=False,
        max_iterations=1,
        sleep=_no_sleep,
        check_self_update=False,
        self_update_code_root=str(code_root),
        self_update_started_version=started_version,
        self_update_run_tests=fake_run_tests,
    )

    assert outcome.restart_required is False
