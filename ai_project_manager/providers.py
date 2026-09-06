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
from email.utils import parsedate_to_datetime
from typing import Callable, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Normalize legacy naive timestamps and aware timestamps to UTC.

    Older integrations and deterministic test clocks may still return naive
    datetimes.  Provider retry deadlines are persisted as timezone-aware ISO
    values, and Python refuses to compare the two forms.  Treat a naive value
    as UTC, which is the registry's documented clock convention.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


_DEFAULT_RECHECK_BACKOFF = timedelta(minutes=5)


class ProviderState:
    AVAILABLE = "AVAILABLE"
    LIMITED = "LIMITED"
    ERROR = "ERROR"


def supports_model_selection(name: str) -> bool:
    """Whether ai-orchestrator accepts an explicit model for ``name``.

    Keep this closed: a configured catalog is not evidence that an arbitrary
    provider supports ``--model``.  These names map to ai-orchestrator agents
    whose adapters explicitly implement the requested-model contract.
    """
    return str(name).strip().casefold() in {
        "antigravity", "claude", "claude-code", "codex", "gemini",
    }


# The three distinct kinds of task the PM ever dispatches a provider for
# (see PROJECT_AUDIT_ROADMAP.md section 8.1). Kept as an explicit, closed
# set so an unknown/misspelled task type fails fast in ``model_for_task``
# instead of silently guessing a model.
TASK_INBOX_PLANNING = "inbox_planning"
TASK_IMPLEMENTATION = "implementation"
TASK_AUDIT = "audit"
TASK_TYPES = (TASK_INBOX_PLANNING, TASK_IMPLEMENTATION, TASK_AUDIT)


# Model quality tiers used by the task/complexity classification rules (see
# PROJECT_AUDIT_ROADMAP.md section 8.7). Kept as an explicit closed set for
# the same reason as TASK_TYPES: an unknown/misspelled tier must fail fast
# rather than silently falling back to a guessed catalog index.
MODEL_TIER_ECONOMICAL = "economical"
MODEL_TIER_BALANCED = "balanced"
MODEL_TIER_QUALITY = "quality"
MODEL_TIERS = (MODEL_TIER_ECONOMICAL, MODEL_TIER_BALANCED, MODEL_TIER_QUALITY)


@dataclass
class ProviderStatus:
    name: str
    state: str = ProviderState.AVAILABLE
    retry_after: Optional[datetime] = None
    last_error: Optional[str] = None
    checkpoint: dict = field(default_factory=dict)
    updated_at: datetime = field(default_factory=_utcnow)
    models: tuple[str, ...] = ()
    selected_model: Optional[str] = None
    # Durable per-task-family capability notes. These are intentionally
    # separate from quota/error state: a provider may remain usable for other
    # work while being unsuitable for one audit scope.
    capability_limits: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "retry_after": self.retry_after.isoformat() if self.retry_after else None,
            "last_error": self.last_error,
            "checkpoint": dict(self.checkpoint),
            "updated_at": self.updated_at.isoformat(),
            "models": list(self.models),
            "selected_model": self.selected_model,
            "capability_limits": dict(self.capability_limits),
        }


class ProviderRegistry:
    """In-memory registry of provider availability.

    ``clock`` is injectable so tests can control time deterministically
    instead of sleeping or monkeypatching datetime globally.
    """

    def __init__(self, clock: Callable[[], datetime] = _utcnow):
        self._clock = clock
        self._statuses: dict[str, ProviderStatus] = {}
        self._configured_model_catalogs: set[str] = set()

    def register(self, name: str) -> ProviderStatus:
        if name not in self._statuses:
            self._statuses[name] = ProviderStatus(name=name, updated_at=self._clock())
        return self._statuses[name]

    def configure_models(self, name: str, models: list[str]) -> ProviderStatus:
        """Record an ordered, explicitly configured model catalog.

        Empty and duplicate values are discarded. Dispatch may use this
        catalog only for providers whose adapter supports explicit selection.
        """
        status = self.register(name)
        self._configured_model_catalogs.add(name)
        normalized = tuple(dict.fromkeys(model.strip() for model in models if model.strip()))
        status.models = normalized
        status.selected_model = normalized[0] if normalized else None
        status.updated_at = self._clock()
        return status

    def has_configured_model_catalog(self, name: str) -> bool:
        """Whether the current process supplied a model catalog for ``name``.

        This distinguishes an intentional empty catalog (the production
        provider-owned selection policy) from a standalone registry that has
        no current configuration and may still need to load legacy state.
        """
        return name in self._configured_model_catalogs

    def selected_model(self, name: str) -> Optional[str]:
        status = self._statuses.get(name)
        return status.selected_model if status is not None else None

    def model_for_task(self, name: str, task_type: str) -> Optional[str]:
        """Resolve a task family to an entry in the configured catalog.

        A provider with zero or one configured models behaves exactly like
        ``selected_model`` for every task type - this only differentiates
        once an operator has actually configured more than one usable model
        in ``AI_PM_PROVIDER_MODELS``, so it never changes behavior outside
        that opt-in case. The ordered model list runs from the most
        economical default (index 0 - Inbox planning and routine
        implementation) to the highest-quality/most capable option (last
        index - the independent audit gate, where correctness matters more
        than throughput; see PROJECT_AUDIT_ROADMAP.md section 8.4).
        """
        if task_type not in TASK_TYPES:
            raise ValueError(f"unknown task_type {task_type!r}; expected one of {TASK_TYPES}")
        status = self._statuses.get(name)
        if status is None or not status.models:
            return None
        if task_type == TASK_AUDIT:
            return status.models[-1]
        return status.models[0]

    def model_for_tier(self, name: str, model_tier: str) -> Optional[str]:
        """Return the legacy catalog suggestion for an explicit quality tier.

        Diagnostics-only, same status as ``model_for_task``: production
        dispatch never forwards this as ``--model``. This generalizes
        ``model_for_task``'s economical/quality split with a ``"balanced"``
        middle tier so the task/complexity classification rules in
        ``task_classification.py`` (PROJECT_AUDIT_ROADMAP.md section 8.7) can
        ask for a tier directly instead of a task type. A provider with fewer
        than three configured models has no real middle entry, so
        ``"balanced"`` falls back to the economical default (index 0) rather
        than guessing.
        """
        if model_tier not in MODEL_TIERS:
            raise ValueError(f"unknown model_tier {model_tier!r}; expected one of {MODEL_TIERS}")
        status = self._statuses.get(name)
        if status is None or not status.models:
            return None
        if model_tier == MODEL_TIER_QUALITY:
            return status.models[-1]
        if model_tier == MODEL_TIER_BALANCED and len(status.models) >= 3:
            return status.models[len(status.models) // 2]
        return status.models[0]

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
            return _as_utc(self._clock()) + retry_after
        return _as_utc(retry_after)

    def is_due_for_recheck(self, name: str) -> bool:
        status = self.get_status(name)
        if status.state == ProviderState.AVAILABLE:
            return False
        if status.retry_after is None:
            return False
        return _as_utc(self._clock()) >= _as_utc(status.retry_after)

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
            # Do not call a broken probe again on every scheduler tick.
            return self.mark_error(
                name,
                str(exc),
                retry_after=_DEFAULT_RECHECK_BACKOFF,
                checkpoint=status.checkpoint,
            )
        if ok:
            resumed_checkpoint = dict(status.checkpoint)
            status = self.mark_available(name)
            status.checkpoint = resumed_checkpoint
            return status
        # Still not available; keep it gated a bit longer.
        return self.mark_error(
            name,
            "recheck probe returned false",
            retry_after=_DEFAULT_RECHECK_BACKOFF,
            checkpoint=status.checkpoint,
        )

    def available_providers(self, names: Optional[list[str]] = None) -> list[str]:
        candidates = names if names is not None else self.registered_names()
        return [n for n in candidates if self.is_available(n)]

    def mark_capability_limited(self, name: str, capability_key: str, reason: str) -> ProviderStatus:
        status = self.get_status(name)
        status.capability_limits[capability_key] = {
            "reason": reason,
            "updated_at": self._clock().isoformat(),
        }
        status.updated_at = self._clock()
        return status

    def is_capability_limited(self, name: str, capability_key: Optional[str]) -> bool:
        if not capability_key:
            return False
        return capability_key in self.get_status(name).capability_limits

    def registered_names(self) -> list[str]:
        return list(self._statuses.keys())


# ---- Limit detection -------------------------------------------------

_LIMIT_PATTERNS = re.compile(
    r"rate limit|quota|too many requests|\b429\b|session limit|usage limit|limit exceeded",
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

    is_limit = str(status_code) == "429" or bool(_LIMIT_PATTERNS.search(message))
    if not is_limit:
        return None

    if retry_after_header is not None:
        try:
            seconds = int(retry_after_header)
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(retry_after_header))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                seconds = max(0, (retry_at - _utcnow()).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
            else:
                return timedelta(seconds=seconds)
        else:
            # A malformed negative delta must not make a limited provider
            # immediately eligible for another request.
            return timedelta(seconds=max(0, seconds))

    return _DEFAULT_LIMIT_BACKOFF
