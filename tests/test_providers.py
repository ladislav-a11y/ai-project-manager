from datetime import datetime, timedelta, timezone

import pytest

from ai_project_manager.providers import ProviderRegistry, ProviderState, detect_limit


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


def test_recheck_probe_raising_marks_error_and_preserves_checkpoint(clock):
    registry = ProviderRegistry(clock=clock)
    registry.mark_limited("claude", retry_after=timedelta(minutes=30), checkpoint={"step": 3})
    clock.advance(timedelta(minutes=31))

    def probe():
        raise RuntimeError("still down")

    status = registry.recheck("claude", probe)

    assert status.state == ProviderState.ERROR
    assert status.checkpoint == {"step": 3}


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
