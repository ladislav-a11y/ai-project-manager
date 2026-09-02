import os

from ai_project_manager.artifact_cleanup import cleanup_test_artifacts
from ai_project_manager import daemon
from ai_project_manager.providers import ProviderRegistry
from ai_project_manager.runner import RunOutcome


def test_cleanup_removes_only_expired_direct_pytest_artifacts(tmp_path):
    stale = tmp_path / ".pytest-basetemp-stale"
    fresh = tmp_path / ".pytest-basetemp-fresh"
    source = tmp_path / "ai_project_manager"
    stale.mkdir()
    fresh.mkdir()
    source.mkdir()
    (stale / "result.tmp").write_text("temporary", encoding="utf-8")
    os.utime(stale, (10, 10))
    os.utime(fresh, (99, 99))

    removed = cleanup_test_artifacts(
        tmp_path, retention_seconds=10, active_run=False, now=lambda: 100
    )

    assert removed == [stale]
    assert not stale.exists()
    assert fresh.exists()
    assert source.exists()


def test_cleanup_is_noop_during_active_run(tmp_path):
    artifact = tmp_path / ".pytest-basetemp-active"
    artifact.mkdir()
    os.utime(artifact, (1, 1))

    assert cleanup_test_artifacts(
        tmp_path, retention_seconds=0, active_run=True, now=lambda: 100
    ) == []
    assert artifact.exists()


def test_cleanup_preserves_files_and_nonmatching_directories(tmp_path):
    file_artifact = tmp_path / ".pytest-basetemp-file"
    unrelated = tmp_path / ".pytest-tmp-old"
    file_artifact.write_text("keep", encoding="utf-8")
    unrelated.mkdir()

    assert cleanup_test_artifacts(
        tmp_path, retention_seconds=0, active_run=False, now=lambda: 100
    ) == []
    assert file_artifact.exists()
    assert unrelated.exists()


def test_scheduler_cleanup_runs_after_tick_has_returned(tmp_path, monkeypatch):
    artifact = tmp_path / ".pytest-basetemp-completed-run"
    artifact.mkdir()
    os.utime(artifact, (1, 1))
    tick_finished = False

    def fake_tick(*args, **kwargs):
        nonlocal tick_finished
        tick_finished = True
        return RunOutcome(ran=False, reason="idle")

    def guarded_cleanup(root, *, retention_seconds, active_run):
        assert tick_finished is True
        assert active_run is False
        return cleanup_test_artifacts(
            root, retention_seconds=retention_seconds,
            active_run=active_run, now=lambda: 100,
        )

    monkeypatch.setattr(daemon, "run_tick", fake_tick)
    monkeypatch.setattr(daemon, "cleanup_test_artifacts", guarded_cleanup)

    daemon.run_loop(
        object(), ProviderRegistry(), lambda project, provider: {},
        once=True, check_self_update=False,
        artifact_cleanup_root=str(tmp_path),
        artifact_cleanup_retention_seconds=0,
    )

    assert not artifact.exists()
