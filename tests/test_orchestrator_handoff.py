from ai_project_manager.models import ProjectRecord, ProjectStatus
from ai_project_manager.orchestrator_handoff import (
    build_orchestrator_task,
    dispatch_to_orchestrator,
    needs_orchestrator_handoff,
)


def test_short_task_without_checkpoint_does_not_need_handoff():
    project = ProjectRecord(name="Demo", orchestrator_ready_task="Fix a typo")
    assert needs_orchestrator_handoff(project) is False


def test_long_task_needs_handoff():
    project = ProjectRecord(name="Demo", orchestrator_ready_task="x" * 250)
    assert needs_orchestrator_handoff(project) is True


def test_project_with_existing_checkpoint_always_needs_handoff():
    project = ProjectRecord(name="Demo", orchestrator_ready_task="short", checkpoint={"step": 2})
    assert needs_orchestrator_handoff(project) is True


def test_build_orchestrator_task_is_autonomous_and_carries_dod_and_checkpoint():
    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.IN_PROGRESS,
        main_task="Build the thing",
        next_step="Wire up auth",
        open_feedback=["login button misaligned"],
        orchestrator_ready_task="Implement auth wiring in auth.ts",
        checkpoint={"commit": "abc123", "step": "auth"},
        provider="claude",
    )

    task = build_orchestrator_task(project)

    assert task.mode == "autonomous"
    assert task.project_name == "Demo"
    assert task.task == "Implement auth wiring in auth.ts"
    assert task.checkpoint == {"commit": "abc123", "step": "auth"}
    assert "Wire up auth" in task.definition_of_done
    assert "login button misaligned" in task.definition_of_done
    assert task.provider == "claude"


def test_dispatch_to_orchestrator_resumes_from_checkpoint_and_updates_project():
    project = ProjectRecord(
        name="Demo",
        orchestrator_ready_task="Implement feature X",
        checkpoint={"step": 1},
    )
    seen_checkpoints = []

    def fake_dispatch(task):
        seen_checkpoints.append(task.checkpoint)
        return {
            "checkpoint": {"step": 2},
            "last_output": "Step 1 done",
            "next_step": "Do step 2",
        }

    result = dispatch_to_orchestrator(project, fake_dispatch)

    assert seen_checkpoints == [{"step": 1}]
    assert result["checkpoint"] == {"step": 2}
    assert project.checkpoint == {"step": 2}
    assert project.last_output == "Step 1 done"
    assert project.next_step == "Do step 2"


def test_dispatch_to_orchestrator_records_stop_reason_on_limit():
    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")

    def fake_dispatch(task):
        return {"checkpoint": {"step": 1}, "stop_reason": "provider session limit hit"}

    dispatch_to_orchestrator(project, fake_dispatch)

    assert project.stop_reason == "provider session limit hit"
    assert project.checkpoint == {"step": 1}
