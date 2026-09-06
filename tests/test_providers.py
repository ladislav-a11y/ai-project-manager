from datetime import datetime, timedelta, timezone

import pytest

from ai_project_manager.providers import (
    ProviderRegistry,
    ProviderState,
    TASK_AUDIT,
    TASK_IMPLEMENTATION,
    TASK_INBOX_PLANNING,
    detect_limit,
    supports_model_selection,
)


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_new_provider_defaults_to_available(clock):
    registry = ProviderRegistry(clock=clock)
    registry.register("claude")
    assert registry.is_available("claude") is True
    assert registry.get_status("claude").state == ProviderState.AVAILABLE


def test_provider_model_catalog_selects_first_unique_model(clock):
    registry = ProviderRegistry(clock=clock)

    status = registry.configure_models("codex", [" gpt-5.6 ", "gpt-5.6", "gpt-5.5"])

    assert status.models == ("gpt-5.6", "gpt-5.5")
    assert registry.selected_model("codex") == "gpt-5.6"
    assert status.to_dict()["selected_model"] == "gpt-5.6"


def test_model_for_task_picks_highest_quality_model_for_audit_only(clock):
    registry = ProviderRegistry(clock=clock)
    registry.configure_models("codex", ["gpt-5.6-economy", "gpt-5.6-pro"])

    assert registry.model_for_task("codex", TASK_INBOX_PLANNING) == "gpt-5.6-economy"
    assert registry.model_for_task("codex", TASK_IMPLEMENTATION) == "gpt-5.6-economy"
    assert registry.model_for_task("codex", TASK_AUDIT) == "gpt-5.6-pro"
    # Never changes the general selected_model()/models[0] semantics.
    assert registry.selected_model("codex") == "gpt-5.6-economy"


def test_model_for_task_matches_selected_model_with_zero_or_one_configured_models(clock):
    registry = ProviderRegistry(clock=clock)
    registry.configure_models("claude", ["claude-opus-4-1"])

    for task_type in (TASK_INBOX_PLANNING, TASK_IMPLEMENTATION, TASK_AUDIT):
        assert registry.model_for_task("claude", task_type) == "claude-opus-4-1"

    registry.register("unconfigured")
    for task_type in (TASK_INBOX_PLANNING, TASK_IMPLEMENTATION, TASK_AUDIT):
        assert registry.model_for_task("unconfigured", task_type) is None
    assert registry.model_for_task("never-registered", TASK_AUDIT) is None


def test_model_for_task_rejects_unknown_task_type(clock):
    registry = ProviderRegistry(clock=clock)
    registry.configure_models("codex", ["gpt-5.6-economy", "gpt-5.6-pro"])

    with pytest.raises(ValueError):
        registry.model_for_task("codex", "some-other-task")


def test_supported_providers_use_generic_model_selection_policy():
    assert supports_model_selection("antigravity") is True
    assert supports_model_selection("claude") is True
    assert supports_model_selection("codex") is True
    assert supports_model_selection("unknown-provider") is False


def test_mark_limited_sets_state_and_retry_after(clock):
    registry = ProviderRegistry(clock=clock)
    registry.mark_limited("claude", retry_after=timedelta(minutes=30), checkpoint={"step": 3}, reason="quota exceeded")

    status = registry.get_status("claude")
    assert status.state == ProviderState.LIMITED
    assert status.retry_after == clock.now + timedelta(minutes=30)
    assert status.checkpoint == {"step": 3}
    assert registry.is_available("claude") is False


def test_limited_provider_not_called_before_retry_after(clock):
    registry = ProviderRegistry(clock=clock)
    registry.mark_limited("claude", retry_after=timedelta(minutes=30))

    probe_calls = []

    def probe():
        probe_calls.append(1)
        return True

    clock.advance(timedelta(minutes=10))
    registry.recheck("claude", probe)

    assert probe_calls == []
    assert registry.is_available("claude") is False


def test_recheck_after_retry_after_restores_availability_and_resumes_checkpoint(clock):
    registry = ProviderRegistry(clock=clock)
    registry.mark_limited("claude", retry_after=timedelta(minutes=30), checkpoint={"step": 3})

    clock.advance(timedelta(minutes=31))

    probe_calls = []

    def probe():
        probe_calls.append(1)
        return True

    status = registry.recheck("claude", probe)

    assert probe_calls == [1]
    assert status.state == ProviderState.AVAILABLE
    assert status.checkpoint == {"step": 3}
    assert registry.is_available("claude") is True


def test_recheck_still_failing_keeps_provider_gated(clock):
    registry = ProviderRegistry(clock=clock)
    registry.mark_limited("claude", retry_after=timedelta(minutes=30), checkpoint={"step": 3})
    clock.advance(timedelta(minutes=31))

    status = registry.recheck("claude", probe=lambda: False)

    assert status.state == ProviderState.ERROR
    assert registry.is_available("claude") is False
    assert status.checkpoint == {"step": 3}
    assert status.retry_after == clock.now + timedelta(minutes=5)

    probe_calls = []
    registry.recheck("claude", probe=lambda: probe_calls.append(1) or True)
    assert probe_calls == []


def test_recheck_probe_raising_marks_error_and_preserves_checkpoint(clock):
    registry = ProviderRegistry(clock=clock)
    registry.mark_limited("claude", retry_after=timedelta(minutes=30), checkpoint={"step": 3})
    clock.advance(timedelta(minutes=31))

    def probe():
        raise RuntimeError("still down")

    status = registry.recheck("claude", probe)

    assert status.state == ProviderState.ERROR
    assert status.checkpoint == {"step": 3}
    assert status.retry_after == clock.now + timedelta(minutes=5)

    probe_calls = []
    registry.recheck("claude", probe=lambda: probe_calls.append(1) or True)
    assert probe_calls == []


def test_available_providers_filters_correctly(clock):
    registry = ProviderRegistry(clock=clock)
    registry.mark_available("claude")
    registry.mark_limited("gpt", retry_after=timedelta(minutes=30))
    registry.mark_error("gemini", "boom")

    assert registry.available_providers(["claude", "gpt", "gemini"]) == ["claude"]


def test_detect_limit_recognizes_common_patterns():
    class FakeHttpError(Exception):
        status_code = 429

    assert detect_limit(FakeHttpError("Too Many Requests")) == timedelta(minutes=30)
    assert detect_limit(RuntimeError("quota exceeded for this session")) == timedelta(minutes=30)
    assert detect_limit(RuntimeError("file not found")) is None


def test_detect_limit_honors_retry_after_header():
    err = RuntimeError("rate limit exceeded")
    result = detect_limit(err, headers={"Retry-After": "120"})
    assert result == timedelta(seconds=120)
