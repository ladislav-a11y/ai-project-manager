from datetime import timedelta

from ai_project_manager.models import ProjectRecord, ProjectStatus
from ai_project_manager.providers import ProviderRegistry
from ai_project_manager.scheduler import pick_next_project


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


def test_skips_blocked_projects_even_if_higher_priority():
    projects = [
        ProjectRecord(name="Blocked", priority=5, status=ProjectStatus.BLOCKED, blocked_by="waiting on design"),
        ProjectRecord(name="Runnable", priority=2, status=ProjectStatus.READY),
    ]
    registry = make_registry(claude="AVAILABLE")

    decision = pick_next_project(projects, registry, default_providers=["claude"])

    assert decision.project.name == "Runnable"


def test_skips_done_and_paused_projects():
    projects = [
        ProjectRecord(name="Done", priority=5, status=ProjectStatus.DONE),
        ProjectRecord(name="Paused", priority=5, status=ProjectStatus.PAUSED),
        ProjectRecord(name="Ready", priority=1, status=ProjectStatus.READY),
    ]
    registry = make_registry(claude="AVAILABLE")

    decision = pick_next_project(projects, registry, default_providers=["claude"])

    assert decision.project.name == "Ready"


def test_returns_none_when_no_provider_available():
    projects = [ProjectRecord(name="Only", priority=5, status=ProjectStatus.READY)]
    registry = make_registry(claude="LIMITED")

    decision = pick_next_project(projects, registry, default_providers=["claude"])

    assert decision is None


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
