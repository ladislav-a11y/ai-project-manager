"""Provider registry: tracks whether each AI provider is usable right now.

States:
  AVAILABLE - safe to call.
  LIMITED   - hit a session/quota limit; do not call again until
              retry_after has passed.
  ERROR     - failed for another reason; also gated by retry_after so we
              don't hammer a broken provider.

Checking/updating status is a pure local operation (dict lookups plus a
clock) so the scheduler can poll it continuously without spending any
AI tokens. Re-verifying an actual provider after retry_after (calling
its API) is an explicit, separate step (``recheck``) so callers control
exactly when a real network/AI call happens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProviderState:
    AVAILABLE = "AVAILABLE"
    LIMITED = "LIMITED"
    ERROR = "ERROR"


@dataclass
class ProviderStatus:
    name: str
    state: str = ProviderState.AVAILABLE
    retry_after: Optional[datetime] = None
    last_error: Optional[str] = None
    checkpoint: dict = field(default_factory=dict)
    updated_at: datetime = field(default_factory=_utcnow)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "retry_after": self.retry_after.isoformat() if self.retry_after else None,
            "last_error": self.last_error,
            "checkpoint": dict(self.checkpoint),
            "updated_at": self.updated_at.isoformat(),
        }


class ProviderRegistry:
    """In-memory registry of provider availability.

    ``clock`` is injectable so tests can control time deterministically
    instead of sleeping or monkeypatching datetime globally.
    """

    def __init__(self, clock: Callable[[], datetime] = _utcnow):
        self._clock = clock
        self._statuses: dict[str, ProviderStatus] = {}

    def register(self, name: str) -> ProviderStatus:
        if name not in self._statuses:
            self._statuses[name] = ProviderStatus(name=name, updated_at=self._clock())
        return self._statuses[name]

    def get_status(self, name: str) -> ProviderStatus:
        return self._statuses.setdefault(name, ProviderStatus(name=name, updated_at=self._clock()))

    def mark_available(self, name: str) -> ProviderStatus:
        status = self.get_status(name)
        status.state = ProviderState.AVAILABLE
        status.retry_after = None
        status.last_error = None
        status.updated_at = self._clock()
        return status

    def mark_limited(
        self,
        name: str,
        retry_after: datetime | timedelta,
        checkpoint: Optional[dict] = None,
        reason: Optional[str] = None,
    ) -> ProviderStatus:
        """Record that a provider hit a session/quota limit. No further
        calls to it should happen until retry_after; the caller's
        in-flight progress is preserved via ``checkpoint``."""
        status = self.get_status(name)
        status.state = ProviderState.LIMITED
        status.retry_after = self._resolve_retry_after(retry_after)
        status.last_error = reason
        if checkpoint is not None:
            status.checkpoint = dict(checkpoint)
        status.updated_at = self._clock()
        return status

    def mark_error(
        self,
        name: str,
        error: str,
        retry_after: Optional[datetime | timedelta] = None,
        checkpoint: Optional[dict] = None,
    ) -> ProviderStatus:
        status = self.get_status(name)
        status.state = ProviderState.ERROR
        status.retry_after = self._resolve_retry_after(retry_after) if retry_after else self._clock()
        status.last_error = error
        if checkpoint is not None:
            status.checkpoint = dict(checkpoint)
        status.updated_at = self._clock()
        return status

    def _resolve_retry_after(self, retry_after: datetime | timedelta) -> datetime:
        if isinstance(retry_after, timedelta):
            return self._clock() + retry_after
        return retry_after

    def is_due_for_recheck(self, name: str) -> bool:
        status = self.get_status(name)
        if status.state == ProviderState.AVAILABLE:
            return False
        if status.retry_after is None:
            return False
        return self._clock() >= status.retry_after

    def is_available(self, name: str) -> bool:
        """Cheap, local availability check - no network/AI call. Does
        NOT auto-flip LIMITED/ERROR to AVAILABLE; that only happens
        through an explicit, successful ``recheck``. A provider that was
        never registered/marked is treated as unavailable rather than
        silently auto-vivified as AVAILABLE - unknown providers must
        never be selected by the scheduler."""
        status = self._statuses.get(name)
        return status is not None and status.state == ProviderState.AVAILABLE

    def recheck(self, name: str, probe: Callable[[], bool]) -> ProviderStatus:
        """After retry_after has passed, actually re-verify the provider
        by calling ``probe`` (real health check / cheap API call) and
        resume from the last checkpoint on success.

        ``probe`` is only invoked when the provider is due for a
        recheck, so we never call a still-limited/erroring provider
        early.
        """
        status = self.get_status(name)
        if not self.is_due_for_recheck(name):
            return status
        try:
            ok = probe()
        except Exception as exc:  # noqa: BLE001 - provider probes may raise anything
            return self.mark_error(name, str(exc), retry_after=None, checkpoint=status.checkpoint)
        if ok:
            resumed_checkpoint = dict(status.checkpoint)
            status = self.mark_available(name)
            status.checkpoint = resumed_checkpoint
            return status
        # Still not available; keep it gated a bit longer.
        return self.mark_error(name, "recheck probe returned false", retry_after=timedelta(minutes=5), checkpoint=status.checkpoint)

    def available_providers(self, names: Optional[list[str]] = None) -> list[str]:
        candidates = names if names is not None else self.registered_names()
        return [n for n in candidates if self.is_available(n)]

    def registered_names(self) -> list[str]:
        return list(self._statuses.keys())


# ---- Limit detection -------------------------------------------------

_LIMIT_PATTERNS = re.compile(
    r"rate limit|quota|too many requests|429|session limit|usage limit|limit exceeded",
    re.IGNORECASE,
)
_DEFAULT_LIMIT_BACKOFF = timedelta(minutes=30)


def detect_limit(exc: BaseException, headers: Optional[dict] = None) -> Optional[timedelta]:
    """Inspect an exception (and optional HTTP headers) to decide
    whether it represents a session/quota limit, and if so how long to
    back off. Returns None when this doesn't look like a limit error.
    """
    message = str(exc)
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)

    retry_after_header = None
    if headers:
        retry_after_header = headers.get("Retry-After") or headers.get("retry-after")

    is_limit = status_code == 429 or bool(_LIMIT_PATTERNS.search(message))
    if not is_limit:
        return None

    if retry_after_header is not None:
        try:
            return timedelta(seconds=int(retry_after_header))
        except (TypeError, ValueError):
            pass

    return _DEFAULT_LIMIT_BACKOFF
