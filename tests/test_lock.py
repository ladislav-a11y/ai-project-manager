from datetime import datetime, timedelta, timezone

import pytest

from ai_project_manager.lock import ProjectLockError, ProjectLockManager


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


def test_acquire_and_release_round_trip(clock):
    manager = ProjectLockManager(clock=clock)
    manager.acquire("Demo", "claude")
    assert manager.is_locked("Demo") is True

    manager.release("Demo", "claude")
    assert manager.is_locked("Demo") is False


def test_second_provider_cannot_acquire_a_live_lock(clock):
    manager = ProjectLockManager(clock=clock)
    manager.acquire("Demo", "claude")

    with pytest.raises(ProjectLockError):
        manager.acquire("Demo", "gpt")


def test_lock_is_released_on_error_inside_hold(clock):
    manager = ProjectLockManager(clock=clock)

    with pytest.raises(RuntimeError):
        with manager.hold("Demo", "claude"):
            assert manager.is_locked("Demo") is True
            raise RuntimeError("boom")

    assert manager.is_locked("Demo") is False


def test_expired_lock_can_be_stolen_by_another_holder(clock):
    manager = ProjectLockManager(clock=clock, default_timeout=timedelta(minutes=10))
    manager.acquire("Demo", "claude")

    clock.advance(timedelta(minutes=11))

    assert manager.is_locked("Demo") is False
    handle = manager.acquire("Demo", "gpt")
    assert handle.holder == "gpt"


def test_releasing_a_lock_you_do_not_hold_is_a_no_op(clock):
    manager = ProjectLockManager(clock=clock, default_timeout=timedelta(minutes=10))
    manager.acquire("Demo", "claude")

    clock.advance(timedelta(minutes=11))
    manager.acquire("Demo", "gpt")

    # The original holder's lock expired and was taken over; its release
    # call must not tear down gpt's now-live lock.
    manager.release("Demo", "claude")
    assert manager.is_locked("Demo") is True


def test_same_holder_can_reacquire_to_refresh_expiry(clock):
    manager = ProjectLockManager(clock=clock, default_timeout=timedelta(minutes=10))
    manager.acquire("Demo", "claude")
    clock.advance(timedelta(minutes=5))
    manager.acquire("Demo", "claude")

    clock.advance(timedelta(minutes=6))
    assert manager.is_locked("Demo") is True
