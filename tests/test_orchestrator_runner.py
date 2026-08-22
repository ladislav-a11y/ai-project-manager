import json
import subprocess

import pytest

from ai_project_manager.models import ProjectRecord, ProjectStatus
from ai_project_manager.orchestrator_runner import OrchestratorProcessError, build_run_fn
from ai_project_manager.providers import ProviderRegistry, ProviderState


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_fn_dispatches_autonomous_task_with_goal_and_checkpoint():
    seen = {}

    def fake_subprocess_run(command, task_json):
        seen["command"] = command
        seen["task"] = json.loads(task_json)
        return completed(stdout=json.dumps({"checkpoint": {"step": 2}, "last_output": "did work", "next_step": "next", "status": "in_progress"}))

    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.READY,
        orchestrator_ready_task="Implement feature X",
        next_step="Wire up auth",
        checkpoint={"step": 1},
    )
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn = build_run_fn(registry, command=["ai-orchestrator"], subprocess_run=fake_subprocess_run)
    result = run_fn(project, "claude")

    assert seen["command"] == ["ai-orchestrator"]
    assert seen["task"]["mode"] == "autonomous"
    assert seen["task"]["task"] == "Implement feature X"
    assert seen["task"]["checkpoint"] == {"step": 1}
    assert "Wire up auth" in seen["task"]["definition_of_done"]

    assert result["checkpoint"] == {"step": 2}
    assert result["last_output"] == "did work"
    assert result["next_step"] == "next"
    assert result["status"] == "in_progress"


def test_run_fn_marks_done_when_orchestrator_reports_done():
    def fake_subprocess_run(command, task_json):
        return completed(stdout=json.dumps({"done": True, "last_output": "shipped"}))

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn = build_run_fn(registry, command=["ai-orchestrator"], subprocess_run=fake_subprocess_run)
    result = run_fn(project, "claude")

    assert result["status"] == "done"
    assert result["last_output"] == "shipped"


def test_run_fn_detects_limit_from_nonzero_exit_and_updates_registry():
    def fake_subprocess_run(command, task_json):
        return completed(stderr="Error: session limit exceeded, try later", returncode=1)

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X", checkpoint={"step": 4})
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn = build_run_fn(registry, command=["ai-orchestrator"], subprocess_run=fake_subprocess_run)
    result = run_fn(project, "claude")

    status = registry.get_status("claude")
    assert status.state == ProviderState.LIMITED
    assert status.retry_after is not None
    # Stays schedulable (not PAUSED) so it auto-resumes once the
    # provider is available again - the registry gates retries, not the
    # project status.
    assert result["status"] == "in_progress"
    assert "session limit" in result["stop_reason"]
    assert result["retry_after"] == status.retry_after.isoformat()


def test_run_fn_detects_limit_reported_explicitly_in_json_payload():
    def fake_subprocess_run(command, task_json):
        payload = {
            "limit_hit": "quota exceeded",
            "retry_after_seconds": 120,
            "checkpoint": {"step": 7},
        }
        return completed(stdout=json.dumps(payload))

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn = build_run_fn(registry, command=["ai-orchestrator"], subprocess_run=fake_subprocess_run)
    result = run_fn(project, "claude")

    status = registry.get_status("claude")
    assert status.state == ProviderState.LIMITED
    assert status.checkpoint == {"step": 7}
    assert result["checkpoint"] == {"step": 7}
    assert result["status"] == "in_progress"


def test_run_fn_raises_on_non_limit_failure_without_touching_registry():
    def fake_subprocess_run(command, task_json):
        return completed(stderr="unexpected crash: NullPointerException", returncode=1)

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn = build_run_fn(registry, command=["ai-orchestrator"], subprocess_run=fake_subprocess_run)

    with pytest.raises(OrchestratorProcessError):
        run_fn(project, "claude")

    assert registry.get_status("claude").state == ProviderState.AVAILABLE


def test_run_fn_raises_on_non_json_output():
    def fake_subprocess_run(command, task_json):
        return completed(stdout="not json at all")

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn = build_run_fn(registry, command=["ai-orchestrator"], subprocess_run=fake_subprocess_run)

    with pytest.raises(OrchestratorProcessError):
        run_fn(project, "claude")


def test_run_fn_detects_limit_from_raised_subprocess_exception():
    def fake_subprocess_run(command, task_json):
        raise RuntimeError("429 too many requests")

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn = build_run_fn(registry, command=["ai-orchestrator"], subprocess_run=fake_subprocess_run)
    result = run_fn(project, "claude")

    assert registry.get_status("claude").state == ProviderState.LIMITED
    assert result["status"] == "in_progress"
