from datetime import timedelta

from ai_project_manager.models import ProjectRecord, ProjectStatus
from ai_project_manager.providers import ProviderRegistry
from ai_project_manager.scheduler import is_schedulable, pick_next_audit_project, pick_next_project


def test_waiting_card_blocks_other_work_until_workflow_resumes():
    # The waiting card is genuinely blocked (physical status), not merely
    # named as waiting - the Trello list, never the title, is authoritative.
    registry = ProviderRegistry()
    registry.mark_available("auto")
    projects = [
        ProjectRecord(
            name="P5 — Station Agent (čeká)",
            priority=5,
            main_task="waiting",
            status=ProjectStatus.BLOCKED,
            blocked_by="waiting on provider",
        ),
        ProjectRecord(name="P5 — Úklid a Git checkpoint ai-orchestrator", priority=5, main_task="run cleanup"),
        ProjectRecord(name="P0 - Řídicí systém", priority=0, orchestrator_ready_task="run control task"),
    ]

    decision = pick_next_project(projects, registry, default_providers=["auto"])

    assert decision is None


def test_ready_card_with_waiting_style_title_is_schedulable():
    # "(čeká)" in the title alone must never veto scheduling - only the
    # physical list (ProjectRecord.status) decides.
    project = ProjectRecord(
        name="P5 — STATION AGENT (ČEKÁ NA AUDIT)",
        priority=5,
        main_task="waiting",
        status=ProjectStatus.READY,
    )

    assert is_schedulable(project) is True


def test_ready_card_with_stale_blocked_by_is_schedulable():
    project = ProjectRecord(
        name="P5 — Station Agent",
        priority=5,
        main_task="ship it",
        status=ProjectStatus.READY,
        blocked_by="stale reason from before the card moved to Ready",
    )

    assert is_schedulable(project) is True


def test_current_status_card_is_not_schedulable():
    project = ProjectRecord(
        name="P5 — Stav projektu",
        priority=5,
        main_task="Účel: zachytit aktuální stav projektu pro operátora.",
    )

    assert is_schedulable(project) is False


def test_executable_task_that_mentions_current_status_remains_schedulable():
    project = ProjectRecord(
        name="P5 — Aktualizace projektu",
        priority=5,
        main_task="Aktualizuj aktuální stav implementace a spusť testy.",
    )

    assert is_schedulable(project) is True

def make_registry(**states):
    registry = ProviderRegistry()
    for name, state in states.items():
        if state == "AVAILABLE":
            registry.mark_available(name)
        elif state == "LIMITED":
            registry.mark_limited(name, retry_after=timedelta(minutes=30))
        elif state == "ERROR":
            registry.mark_error(name, "boom")
    return registry


def test_picks_highest_priority_unblocked_project():
    projects = [
        ProjectRecord(name="Low", priority=1, status=ProjectStatus.READY),
        ProjectRecord(name="High", priority=5, status=ProjectStatus.READY),
        ProjectRecord(name="Mid", priority=3, status=ProjectStatus.READY),
    ]
    registry = make_registry(claude="AVAILABLE")

    decision = pick_next_project(projects, registry, default_providers=["claude"])

    assert decision is not None
    assert decision.project.name == "High"
    assert decision.provider == "claude"


def test_auto_alias_selects_first_available_real_provider():
    registry = make_registry(
        auto="AVAILABLE",
        antigravity="LIMITED",
        claude="AVAILABLE",
        codex="AVAILABLE",
    )
    project = ProjectRecord(name="Auto", priority=5, status=ProjectStatus.READY)

    decision = pick_next_project([project], registry, default_providers=["auto"])

    assert decision is not None
    assert decision.provider == "claude"


def test_continues_in_progress_before_higher_priority_ready_card():
    projects = [
        ProjectRecord(name="P5 - new", priority=5, status=ProjectStatus.READY),
        ProjectRecord(name="P1 - active", priority=1, status=ProjectStatus.IN_PROGRESS),
    ]
    registry = make_registry(claude="AVAILABLE")

    decision = pick_next_project(projects, registry, default_providers=["claude"])

    assert decision is not None
    assert decision.project.name == "P1 - active"


def test_returned_from_testing_precedes_normal_work_even_at_same_priority():
    projects = [
        ProjectRecord(name="Normal P5", priority=5, status=ProjectStatus.IN_PROGRESS),
        ProjectRecord(
            name="Returned P5",
            priority=5,
            status=ProjectStatus.IN_PROGRESS,
            extra_data={"returned_from_testing": True},
        ),
    ]
    registry = make_registry(claude="AVAILABLE")

    decision = pick_next_project(projects, registry, default_providers=["claude"])

    assert decision is not None
    assert decision.project.name == "Returned P5"


def test_blocked_workflow_blocks_new_ready_work():
    projects = [
        ProjectRecord(name="Blocked", priority=5, status=ProjectStatus.BLOCKED, blocked_by="waiting on design"),
        ProjectRecord(name="Runnable", priority=2, status=ProjectStatus.READY),
    ]
    registry = make_registry(claude="AVAILABLE")

    decision = pick_next_project(projects, registry, default_providers=["claude"])

    assert decision is None


def test_paused_workflow_blocks_new_ready_work_until_it_resumes():
    projects = [
        ProjectRecord(name="Done", priority=5, status=ProjectStatus.DONE),
        ProjectRecord(name="Paused", priority=5, status=ProjectStatus.PAUSED),
        ProjectRecord(name="Ready", priority=1, status=ProjectStatus.READY),
    ]
    registry = make_registry(claude="AVAILABLE")

    decision = pick_next_project(projects, registry, default_providers=["claude"])

    assert decision is None


def test_testing_workflow_blocks_new_ready_work_until_audit_finishes():
    projects = [
        ProjectRecord(name="Awaiting independent audit", priority=1, status=ProjectStatus.TESTING),
        ProjectRecord(name="Fresh ready", priority=5, status=ProjectStatus.READY),
    ]
    registry = make_registry(claude="AVAILABLE")

    decision = pick_next_project(projects, registry, default_providers=["claude"])

    assert decision is None


def test_returned_rework_may_repair_human_hold_but_not_open_ready_work():
    projects = [
        ProjectRecord(
            name="Returned repair",
            priority=3,
            status=ProjectStatus.IN_PROGRESS,
            extra_data={"returned_from_testing": True},
        ),
        ProjectRecord(
            name="Human hold",
            priority=5,
            status=ProjectStatus.BLOCKED,
            blocked_by="human decision required",
            human_action_step="review card",
            human_notified_reason="human decision required",
        ),
        ProjectRecord(name="Fresh ready", priority=5, status=ProjectStatus.READY),
    ]
    registry = make_registry(claude="AVAILABLE")

    decision = pick_next_project(projects, registry, default_providers=["claude"])

    assert decision is not None
    assert decision.project.name == "Returned repair"


def test_returns_none_when_no_provider_available():
    projects = [ProjectRecord(name="Only", priority=5, status=ProjectStatus.READY)]
    registry = make_registry(claude="LIMITED")

    decision = pick_next_project(projects, registry, default_providers=["claude"])

    assert decision is None


def test_dependency_ready_child_waits_for_done_sibling_even_with_higher_priority():
    registry = make_registry(claude="AVAILABLE")
    dependency = ProjectRecord(
        name="Base",
        priority=1,
        status=ProjectStatus.READY,
        extra_data={"inbox_preparation": {"source_card_id": "source", "subtask_index": 0}},
    )
    dependent = ProjectRecord(
        name="Dependent",
        priority=5,
        status=ProjectStatus.READY,
        extra_data={"inbox_preparation": {
            "source_card_id": "source", "subtask_index": 1,
            "depends_on_subtask_indices": [0],
        }},
    )
    assert pick_next_project([dependency, dependent], registry, default_providers=["claude"]).project is dependency
    dependency.status = ProjectStatus.DONE
    assert pick_next_project([dependency, dependent], registry, default_providers=["claude"]).project is dependent


def test_falls_back_to_lower_priority_project_when_top_providers_unavailable():
    projects = [
        ProjectRecord(name="High", priority=5, status=ProjectStatus.READY),
        ProjectRecord(name="Low", priority=1, status=ProjectStatus.READY),
    ]
    registry = make_registry(claude="LIMITED", gpt="AVAILABLE")
    providers_for_project = {
        "High": ["claude"],
        "Low": ["gpt"],
    }

    decision = pick_next_project(projects, registry, providers_for_project=providers_for_project)

    assert decision.project.name == "Low"
    assert decision.provider == "gpt"


def test_scheduler_never_touches_providers_that_are_not_registered():
    # A project referencing an unknown provider should simply be skipped,
    # never crash and never trigger any provider/AI call.
    projects = [ProjectRecord(name="Solo", priority=4, status=ProjectStatus.READY)]
    registry = ProviderRegistry()

    decision = pick_next_project(projects, registry, default_providers=["unregistered"])

    assert decision is None


def test_audit_capability_limit_skips_provider_for_same_task_family():
    project = ProjectRecord(
        name="P5.04 — propagation a scoring",
        project_key="Station Agent",
        priority=5.04,
        status=ProjectStatus.TESTING,
        extra_data={"inbox_preparation": {"scope": "propagation a scoring"}},
    )
    registry = make_registry(antigravity="AVAILABLE", codex="AVAILABLE")
    registry.mark_capability_limited(
        "antigravity",
        "audit:station agent:propagation a scoring",
        "no independent verdict",
    )

    decision = pick_next_audit_project(
        [project], registry, default_providers=["antigravity", "codex"]
    )

    assert decision is not None
    assert decision.provider == "codex"


def test_audit_only_rejection_waits_for_change_instead_of_repeating_the_same_audit():
    project = ProjectRecord(
        name="Held audit",
        priority=5,
        status=ProjectStatus.TESTING,
        extra_data={"audit_waiting_for_change": True},
    )
    registry = make_registry(claude="AVAILABLE", codex="AVAILABLE")

    assert pick_next_audit_project(
        [project], registry, default_providers=["claude", "codex"]
    ) is None
