"""Project locking: prevents two providers from concurrently modifying
the same project.

A lock is a plain, local, in-memory reservation with an expiry, so a
crashed or hung holder can never wedge a project forever - once
``expires_at`` passes, the lock is treated as free and can be
re-acquired even if the original holder never called ``release``.
``ProjectLockManager.hold`` is a context manager that guarantees
release on a normal exit *and* on any exception, which is the
supported way to run work under a project lock.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


DEFAULT_LOCK_TIMEOUT = timedelta(minutes=15)


class ProjectLockError(RuntimeError):
    """Raised when a project is already locked by a different holder."""


@dataclass
class LockHandle:
    project_name: str
    holder: str
    expires_at: datetime


class ProjectLockManager:
    """In-memory per-project lock registry.

    ``clock`` is injectable so tests can control expiry deterministically.
    """

    def __init__(
        self,
        clock: Callable[[], datetime] = _utcnow,
        default_timeout: timedelta = DEFAULT_LOCK_TIMEOUT,
    ):
        if default_timeout <= timedelta(0):
            raise ValueError("default_timeout must be positive")
        self._clock = clock
        self._default_timeout = default_timeout
        self._locks: dict[str, LockHandle] = {}

    def _is_expired(self, lock: LockHandle) -> bool:
        return self._clock() >= lock.expires_at

    def is_locked(self, project_name: str) -> bool:
        lock = self._locks.get(project_name)
        return lock is not None and not self._is_expired(lock)

    def is_locked_by_other(self, project_name: str, holder: str) -> bool:
        """Return whether another holder currently owns a live lock.

        The scheduler uses this before choosing work so a busy high-priority
        project cannot starve every lower-priority project.  ``acquire`` still
        performs the authoritative check afterwards, closing the race between
        scheduling and lock acquisition.
        """
        lock = self._locks.get(project_name)
        return (
            lock is not None
            and not self._is_expired(lock)
            and lock.holder != holder
        )

    def acquire(
        self,
        project_name: str,
        holder: str,
        timeout: Optional[timedelta] = None,
    ) -> LockHandle:
        """Reserve ``project_name`` for ``holder``. Raises
        ``ProjectLockError`` if another, still-live holder already has it.
        A holder re-acquiring its own lock simply refreshes the expiry.
        An expired lock is silently taken over by the new holder."""
        existing = self._locks.get(project_name)
        if existing is not None and not self._is_expired(existing) and existing.holder != holder:
            raise ProjectLockError(
                f"project {project_name!r} is locked by {existing.holder!r} "
                f"until {existing.expires_at.isoformat()}"
            )
        effective_timeout = self._default_timeout if timeout is None else timeout
        if effective_timeout <= timedelta(0):
            raise ValueError("timeout must be positive")
        handle = LockHandle(
            project_name=project_name,
            holder=holder,
            expires_at=self._clock() + effective_timeout,
        )
        self._locks[project_name] = handle
        return handle

    def release(self, project_name: str, holder: str) -> None:
        """Release ``project_name`` if ``holder`` currently owns it.
        Releasing a lock you don't hold (already expired and stolen, or
        never acquired) is a safe no-op."""
        existing = self._locks.get(project_name)
        if existing is not None and existing.holder == holder:
            del self._locks[project_name]

    @contextmanager
    def hold(
        self,
        project_name: str,
        holder: str,
        timeout: Optional[timedelta] = None,
    ) -> Iterator[LockHandle]:
        """Acquire the lock for the duration of the ``with`` block and
        guarantee it is released afterwards, whether the block finishes
        normally, raises, or times out logically (the caller decides how
        long is too long; the expiry above is the hard backstop)."""
        handle = self.acquire(project_name, holder, timeout=timeout)
        try:
            yield handle
        finally:
            self.release(project_name, holder)
