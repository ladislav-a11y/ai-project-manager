import pytest

from ai_project_manager.models import ProjectRecord, ProjectStatus
from ai_project_manager.orchestrator_handoff import (
    InvalidTaskError,
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


# ---- building a concrete goal from main_task/open_feedback/next_step
# when orchestrator_ready_task was never filled in (item 2) -------------

def test_build_orchestrator_task_falls_back_to_main_task_feedback_and_next_step():
    project = ProjectRecord(
        name="Demo",
        main_task="Ship the billing export",
        open_feedback=["export times out on large accounts"],
        next_step="add pagination to the export query",
    )

    task = build_orchestrator_task(project)

    assert "Ship the billing export" in task.task
    assert "export times out on large accounts" in task.task
    assert "add pagination to the export query" in task.task
    assert task.task != "{"
    assert "Ship the billing export" in task.definition_of_done
    assert "add pagination to the export query" in task.definition_of_done
    assert "export times out on large accounts" in task.definition_of_done


def test_build_orchestrator_task_prefers_orchestrator_ready_task_over_fallback():
    project = ProjectRecord(
        name="Demo",
        main_task="Ship the billing export",
        orchestrator_ready_task="Implement auth wiring in auth.ts",
    )

    task = build_orchestrator_task(project)

    assert task.task == "Implement auth wiring in auth.ts"


# ---- refusing to spend AI tokens on an empty task/DoD (item 1) --------

def test_build_orchestrator_task_raises_on_completely_empty_project():
    project = ProjectRecord(name="Demo")

    with pytest.raises(InvalidTaskError):
        build_orchestrator_task(project)


def test_build_orchestrator_task_raises_when_explicit_dod_is_all_blank():
    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")

    with pytest.raises(InvalidTaskError):
        build_orchestrator_task(project, definition_of_done=[""])


def test_dispatch_to_orchestrator_never_calls_dispatch_for_an_invalid_task():
    """No AI tokens spent: the dispatcher must not even be invoked when
    the task is invalid."""
    project = ProjectRecord(name="Demo")
    calls = []

    def fake_dispatch(task):
        calls.append(task)
        return {}

    with pytest.raises(InvalidTaskError):
        dispatch_to_orchestrator(project, fake_dispatch)

    assert calls == []


# ---- regression test on the real, observed Station Agent payload
# (item 7): goal must never be "{", task must never be empty, and the
# DoD must never contain a blank string -----------------------------

def test_real_station_agent_payload_regression_empty_project_is_refused():
    """This mirrors the exact broken run that motivated this fix: a
    Trello card for "P5 - Station Agent" with no orchestrator_ready_task,
    no main_task, no next_step and no open_feedback filled in yet. The
    old code silently produced task="" and definition_of_done=[""] and
    handed that straight to ai-orchestrator (goal ended up as a bare
    "{"). It must now be refused outright instead."""
    project = ProjectRecord(name="P5 — Station Agent", status=ProjectStatus.READY)

    with pytest.raises(InvalidTaskError):
        build_orchestrator_task(project)


def test_real_station_agent_payload_regression_filled_card_is_never_a_json_brace():
    project = ProjectRecord(
        name="P5 — Station Agent",
        status=ProjectStatus.READY,
        main_task="Fix the live handoff so autonomous gets a real goal and DoD",
        open_feedback=["autonomous run started with goal '{' and an empty task"],
        next_step="pair outbox results by run id instead of project name",
    )

    task = build_orchestrator_task(project)

    assert task.task.strip() != ""
    assert task.task.strip() != "{"
    assert not task.task.strip().startswith("{")
    assert task.definition_of_done
    assert all(isinstance(item, str) and item.strip() for item in task.definition_of_done)
