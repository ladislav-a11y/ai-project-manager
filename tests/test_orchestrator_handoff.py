import pytest

from ai_project_manager.card_contract import GOVERNANCE_POLICY
from ai_project_manager.models import DoDItem, ProjectRecord, ProjectStatus
from ai_project_manager.orchestrator_handoff import (
    AuditVerdictError,
    InvalidTaskError,
    apply_audit_verdict,
    apply_dod_progress,
    build_audit_task,
    build_orchestrator_task,
    dispatch_to_orchestrator,
    dod_fully_verified,
    needs_orchestrator_handoff,
)


@pytest.mark.parametrize("verdict", ["accepted", "rejected"])
def test_audit_verdict_requires_traceable_evidence(verdict):
    project = ProjectRecord(name="Demo", dod=[DoDItem(text="implementation", checked=True)])

    with pytest.raises(AuditVerdictError, match="requires concrete evidence"):
        apply_audit_verdict(project, verdict, reason="needs work" if verdict == "rejected" else None)


def test_rejected_audit_verdict_requires_a_concrete_reason():
    project = ProjectRecord(name="Demo", dod=[DoDItem(text="implementation", checked=True)])

    with pytest.raises(AuditVerdictError, match="requires a concrete reason"):
        apply_audit_verdict(project, "rejected", reason=None, evidence="ran the suite, X failed")


def test_rejected_implementation_is_reopened_for_actual_rework():
    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.TESTING,
        dod=[DoDItem(text="implementation", checked=True)],
    )

    apply_audit_verdict(
        project,
        "rejected",
        reason="the export still times out on large accounts",
        evidence="ran the export against a 10k-row fixture, it timed out after 30s",
        rejected_indices=[0],
    )

    assert project.status == ProjectStatus.IN_PROGRESS
    assert project.stop_reason == "the export still times out on large accounts"
    assert project.returned_from_testing is True
    assert project.open_feedback == [
        "the export still times out on large accounts\n"
        "Evidence: ran the export against a 10k-row fixture, it timed out after 30s"
    ]
    assert project.dod[0].checked is False
    assert project.checkpoint["completed_dod_indices"] == []


def test_rejected_audit_only_item_does_not_reopen_implementation():
    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.TESTING,
        dod=[
            DoDItem(text="implementation", checked=True),
            DoDItem(text="independent audit", checked=True, phase="audit"),
        ],
        checkpoint={"completed_dod_indices": [0, 1]},
    )

    apply_audit_verdict(
        project,
        "rejected",
        reason="live evidence is stale",
        evidence="audit needs a fresh readback",
        reject_target=ProjectStatus.TESTING,
        rejected_indices=[1],
    )

    assert project.status == ProjectStatus.TESTING
    assert [item.checked for item in project.dod] == [True, True]
    assert project.checkpoint["completed_dod_indices"] == [0, 1]


def test_actionable_audit_rejection_creates_rework_item_and_returns_to_implementation():
    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.TESTING,
        dod=[
            DoDItem(text="implementation", checked=True),
            DoDItem(text="independent audit", checked=True, phase="audit"),
        ],
        checkpoint={"completed_dod_indices": [0, 1]},
    )

    apply_audit_verdict(
        project,
        "rejected",
        reason="live evidence is missing",
        evidence="the audit could not verify the current runtime response",
        rejected_indices=[1],
    )

    assert project.status == ProjectStatus.IN_PROGRESS
    assert project.returned_from_testing is True
    assert project.dod[-1].phase == "implementation"
    assert project.dod[-1].checked is False
    assert project.dod[-1].text == (
        "Nápravný úkol: provést konkrétní nápravnou změnu "
        "vyplývající z poslední námitky a doložit její výsledek"
    )
    assert project.next_step == project.dod[-1].text
    # The rejected audit index is removed from the resumable checkpoint; the
    # new implementation item is intentionally absent until rework completes.
    assert project.checkpoint["completed_dod_indices"] == [0]


def test_rejected_audit_verdict_can_target_ready_for_a_fresh_attempt():
    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.TESTING,
        dod=[DoDItem(text="implementation", checked=True)],
    )

    apply_audit_verdict(
        project,
        "rejected",
        reason="approach is unsalvageable, start over",
        evidence="tried three fixes, all regressed the same test",
        reject_target=ProjectStatus.READY,
    )

    assert project.status == ProjectStatus.READY
    # Only the default IN_PROGRESS target marks the card as corrective rework.
    assert project.returned_from_testing is False


def test_rejected_audit_verdict_can_remain_in_testing_for_audit_only_dod():
    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.TESTING,
        dod=[
            DoDItem(text="implementation", checked=True),
            DoDItem(text="independent audit", phase="audit"),
        ],
    )

    apply_audit_verdict(
        project,
        "rejected",
        reason="live board evidence is missing",
        evidence="the audit could not verify the current Trello card",
        reject_target=ProjectStatus.TESTING,
    )

    assert project.status == ProjectStatus.TESTING
    assert project.returned_from_testing is False
    assert "live board evidence is missing" in project.stop_reason


def test_apply_audit_verdict_rejects_invalid_reject_target():
    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.TESTING,
        dod=[DoDItem(text="implementation", checked=True)],
    )

    with pytest.raises(AuditVerdictError, match="invalid audit reject_target"):
        apply_audit_verdict(
            project,
            "rejected",
            reason="needs work",
            evidence="evidence",
            reject_target=ProjectStatus.DONE,
        )


def test_apply_audit_verdict_rejects_unknown_verdict_string():
    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.TESTING,
        dod=[DoDItem(text="implementation", checked=True)],
    )

    with pytest.raises(AuditVerdictError, match="invalid audit verdict"):
        apply_audit_verdict(project, "maybe", reason="needs work", evidence="evidence")


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
    assert task.governance == GOVERNANCE_POLICY
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


def test_build_orchestrator_task_carries_trello_feedback_on_rework():
    project = ProjectRecord(
        name="Demo",
        orchestrator_ready_task="Implement auth wiring in auth.ts",
        open_feedback=["audit rejected missing recovery test"],
        next_step="add the missing recovery test",
        last_output="Previous attempt changed the adapter but the audit still rejected recovery.",
        extra_data={"returned_from_testing": True},
    )

    task = build_orchestrator_task(project)

    assert "Implement auth wiring in auth.ts" in task.task
    assert "audit rejected missing recovery test" in task.task
    assert "add the missing recovery test" in task.task
    assert "Previous attempt changed the adapter" in task.task


def test_build_orchestrator_task_does_not_repeat_identical_prepared_task_and_next_step():
    project = ProjectRecord(
        name="Demo",
        orchestrator_ready_task="Implement the current slice",
        next_step="Implement the current slice",
        extra_data={"returned_from_testing": True},
    )

    task = build_orchestrator_task(project)

    assert task.task.count("Implement the current slice") == 1


def test_build_orchestrator_task_carries_bounded_error_recovery_context():
    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.ERROR,
        orchestrator_ready_task="Implement the current slice",
        stop_reason="apply_patch verification failed: stale README anchor",
    )

    task = build_orchestrator_task(project)

    assert "Recovery context from the previous failed implementation run" in task.task
    assert "stale README anchor" in task.task
    assert "do not repeat a stale patch" in task.task
    assert "stale README anchor" not in task.definition_of_done


def test_build_orchestrator_task_carries_no_cross_card_learning_memory():
    """Regression: the goal text handed to a fresh run must come only from
    the card's own current contract (task, DoD, feedback, checkpoint) -
    never from strategic/persistent memory accumulated on other cards or
    previous iterations. See ai_project_manager/card_contract.py, which now
    drops any legacy ``learning_context`` before it can reach extra_data.
    """
    project = ProjectRecord(
        name="Demo",
        orchestrator_ready_task="Implement auth wiring in auth.ts",
        extra_data={
            "learning_context": {
                "version": 1,
                "current_strategy": "nejdřív ověřit živý receipt",
                "history": [
                    {
                        "phase": "audit",
                        "result": "rejected",
                        "cause": "missing live evidence",
                        "feedback": ["prove the real CLI contract"],
                        "next_strategy": "nejdřív ověřit živý receipt",
                        "repetitions": 1,
                    }
                ],
            }
        },
    )

    task = build_orchestrator_task(project)

    assert "Projektová paměť z Trella" not in task.task
    assert "missing live evidence" not in task.task
    assert "nejdřív ověřit živý receipt" not in task.task
    assert task.task == "Implement auth wiring in auth.ts"


def test_build_orchestrator_task_extracts_bare_trello_definition_of_done_items():
    project = ProjectRecord(
        name="Audit",
        main_task="Priorita P1. AKTIVNÍ PRACOVNÍ KARTA.",
        orchestrator_ready_task="""Priorita P1. AKTIVNÍ PRACOVNÍ KARTA.

DEFINITION OF DONE
[ ] české Trello seznamy jsou mapovány deterministicky,
[ ] P5 je skutečně vyšší priorita než P0,
[ ] výsledek je synchronizován zpět do Trella.
""",
    )

    task = build_orchestrator_task(project)

    assert task.definition_of_done == [
        "české Trello seznamy jsou mapovány deterministicky",
        "P5 je skutečně vyšší priorita než P0",
        "výsledek je synchronizován zpět do Trella.",
    ]


def test_build_orchestrator_task_extracts_markdown_checklist_and_deduplicates():
    project = ProjectRecord(
        name="Audit",
        main_task="Audit the integration",
        orchestrator_ready_task="- [ ] run all tests\n* [x] verify live handoff\n- [ ] run all tests",
    )

    task = build_orchestrator_task(project)

    assert task.definition_of_done == ["run all tests", "verify live handoff"]


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
# ---- project.dod is the single source of truth once set (item: viditelny
# checklist se parsuje do ProjectRecord/DoD) --------------------------

def test_build_orchestrator_task_prefers_project_dod_over_raw_task_text():
    project = ProjectRecord(
        name="Demo",
        orchestrator_ready_task="- [ ] this text checklist must be ignored",
        dod=[DoDItem(text="bod 1"), DoDItem(text="bod 2"), DoDItem(text="bod 3")],
    )

    task = build_orchestrator_task(project)

    assert task.definition_of_done == ["bod 1", "bod 2", "bod 3"]


def test_implementation_handoff_never_contains_audit_dod():
    project = ProjectRecord(
        name="Demo",
        main_task="Implement feature",
        dod=[
            DoDItem(text="implement feature"),
            DoDItem(text="independent audit accepts evidence", phase="audit"),
        ],
    )

    task = build_orchestrator_task(project)

    assert task.definition_of_done == ["implement feature"]


def test_audit_handoff_receives_audit_dod_only_in_audit_mode():
    project = ProjectRecord(
        name="Demo",
        main_task="Implement feature",
        status=ProjectStatus.TESTING,
        dod=[
            DoDItem(text="implement feature", checked=True),
            DoDItem(text="independent audit accepts evidence", phase="audit"),
        ],
    )

    task = build_audit_task(project)

    assert task.mode == "audit"
    assert task.definition_of_done == ["implement feature", "independent audit accepts evidence"]
    assert task.checkpoint["completed_dod_indices"] == [0, 1]
    assert project.dod[1].checked is False


def test_audit_handoff_includes_fresh_trello_readback_evidence():
    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.TESTING,
        main_task="Implement feature",
        dod=[DoDItem(text="implement feature", checked=True)],
        extra_data={
            "live_trello_readback": {
                "status": "ok",
                "card_id": "card-123",
                "list_name": "Testování",
                "last_activity_at": "2026-08-31T12:00:00+00:00",
            }
        },
    )

    task = build_audit_task(project)

    assert "Fresh live Trello readback" in task.task
    assert "card-123" in task.task
    assert "Testování" in task.task


def test_apply_dod_progress_marks_reported_indices_checked():
    project = ProjectRecord(
        name="Demo",
        dod=[DoDItem(text="a"), DoDItem(text="b"), DoDItem(text="c")],
    )

    apply_dod_progress(project, {"completed_dod_indices": [0, 2]})

    assert [item.checked for item in project.dod] == [True, False, True]


def test_apply_dod_progress_ignores_out_of_range_and_non_int_indices():
    project = ProjectRecord(name="Demo", dod=[DoDItem(text="a")])
    checkpoint = {"completed_dod_indices": [5, "x", None, True, 0, 0]}

    apply_dod_progress(project, checkpoint)

    assert project.dod[0].checked is True
    assert checkpoint["completed_dod_indices"] == [0]


def test_build_task_removes_stale_checkpoint_indices_after_dod_change():
    project = ProjectRecord(
        name="Demo",
        main_task="cil",
        dod=[DoDItem(text="a"), DoDItem(text="b")],
        checkpoint={"completed_dod_indices": [0, 6, 7, 0], "run_id": "same"},
    )

    task = build_orchestrator_task(project)

    assert task.checkpoint == {"completed_dod_indices": [0], "run_id": "same"}
    assert project.checkpoint == task.checkpoint


def test_apply_dod_progress_never_unchecks_a_previously_verified_item():
    project = ProjectRecord(name="Demo", dod=[DoDItem(text="a", checked=True), DoDItem(text="b")])

    apply_dod_progress(project, {"completed_dod_indices": []})

    assert project.dod[0].checked is True


def test_dod_fully_verified_true_when_every_item_checked():
    project = ProjectRecord(
        name="Demo", dod=[DoDItem(text="a", checked=True), DoDItem(text="b", checked=True)]
    )
    assert dod_fully_verified(project) is True


def test_dod_fully_verified_false_when_any_item_unchecked():
    project = ProjectRecord(
        name="Demo", dod=[DoDItem(text="a", checked=True), DoDItem(text="b", checked=False)]
    )
    assert dod_fully_verified(project) is False


def test_dod_fully_verified_vacuously_true_with_no_explicit_checklist():
    project = ProjectRecord(name="Demo")
    assert dod_fully_verified(project) is True


def test_build_orchestrator_task_extracts_inline_trello_definition_of_done_items():
    project = ProjectRecord(
        name="Audit",
        main_task=(
            "CIL: overit waiting stav.\n\n"
            "DEFINITION OF DONE: [ ] waiting propagovan do AI PM "
            "[ ] Slack waiting notifikace "
            "[ ] Trello waiting stav "
            "[ ] pokracovani z checkpointu"
        ),
        orchestrator_ready_task=(
            "CIL: overit waiting stav.\n\n"
            "DEFINITION OF DONE: [ ] waiting propagovan do AI PM "
            "[ ] Slack waiting notifikace "
            "[ ] Trello waiting stav "
            "[ ] pokracovani z checkpointu"
        ),
    )

    task = build_orchestrator_task(project)

    assert task.definition_of_done == [
        "waiting propagovan do AI PM",
        "Slack waiting notifikace",
        "Trello waiting stav",
        "pokracovani z checkpointu",
    ]
