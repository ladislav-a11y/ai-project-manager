"""Persistent storage for AI provider availability state."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import uuid
from pathlib import Path

from .providers import ProviderRegistry, ProviderState

logger = logging.getLogger("ai_project_manager")

_INVALID_STATE_RECHECK_BACKOFF = timedelta(minutes=5)


def _gate_registered_providers_after_invalid_file(
    registry: ProviderRegistry,
    source: Path,
    reason: str,
) -> None:
    """Fail closed when the state file cannot be trusted.

    Providers are normally registered as AVAILABLE before persisted state is
    loaded.  Leaving them that way after a truncated or structurally invalid
    file could immediately repeat a request that had already hit a quota
    limit.  A short ERROR backoff keeps polling local and lets the ordinary
    recheck path recover automatically without human intervention.
    """
    logger.warning(
        "provider state file %s is invalid or corrupted, gating known providers: %s",
        source,
        reason,
    )
    for name in registry.registered_names():
        checkpoint = dict(registry.get_status(name).checkpoint)
        registry.mark_error(
            name,
            f"invalid persisted provider state: {reason}",
            retry_after=_INVALID_STATE_RECHECK_BACKOFF,
            checkpoint=checkpoint,
        )


def save_provider_state(path: str | Path, registry: ProviderRegistry) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    data = {}

    for name, status in registry._statuses.items():
        data[name] = {
            "state": status.state,
            "retry_after": (
                status.retry_after.isoformat()
                if status.retry_after
                else None
            ),
            "last_error": status.last_error,
            "checkpoint": status.checkpoint,
            "models": list(status.models),
            "selected_model": status.selected_model,
            "capability_limits": status.capability_limits,
        }

    # Write to a sibling temp file and atomically rename it into place -
    # a process kill/crash/power loss mid-write must never leave a
    # truncated, half-written provider_state.json behind, since that
    # would crash load_provider_state (and the whole unattended process)
    # on every subsequent startup until a human manually deletes it.
    tmp_target = target.with_name(f"{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        tmp_target.write_text(
            json.dumps(data, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_target, target)
    finally:
        # ``replace`` can fail (permissions, antivirus/file locking, full
        # filesystem).  Never accumulate stale state fragments, while
        # preserving the original exception for the caller.
        tmp_target.unlink(missing_ok=True)


def load_provider_state(path: str | Path, registry: ProviderRegistry) -> None:
    source = Path(path)

    if not source.exists():
        return

    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        # A corrupted/truncated state file (e.g. from a crash that somehow
        # still slipped past the atomic write above, or a manual edit) must
        # neither crash startup nor make a previously limited provider look
        # available. This includes a temporarily unreadable file, invalid
        # UTF-8, and malformed JSON.  An unreadable state is just as
        # untrustworthy as a malformed one: fail closed briefly, then let the
        # normal provider recheck path recover without wedging CLI startup.
        _gate_registered_providers_after_invalid_file(registry, source, str(exc))
        return

    if not isinstance(data, dict):
        _gate_registered_providers_after_invalid_file(
            registry,
            source,
            f"invalid root: expected an object, got {type(data).__name__}",
        )
        return

    # The CLI registers configured providers as AVAILABLE before loading this
    # file.  If one of those providers has a malformed entry, skipping it
    # would therefore fail open and could immediately repeat a paid request
    # that had previously hit a limit.  Invalid entries for providers that
    # are not configured remain ignorable, so stale third-party entries do
    # not silently expand the active registry.
    configured_names = set(registry.registered_names())
    restrict_to_configured = bool(configured_names)

    def gate_configured_entry(name: object, reason: str, checkpoint: object = None) -> None:
        logger.warning("ignoring invalid provider state entry %r in %s: %s", name, source, reason)
        if name not in configured_names:
            return
        safe_checkpoint = dict(checkpoint) if isinstance(checkpoint, Mapping) else {}
        registry.mark_error(
            name,
            f"invalid persisted provider state: {reason}",
            retry_after=_INVALID_STATE_RECHECK_BACKOFF,
            checkpoint=safe_checkpoint,
        )

    def apply_persisted_model_state(name: str, models: list[str], selected_model: object) -> None:
        """Load model metadata only when the process has no current catalog.

        The PM now delegates model selection to providers and production
        config intentionally supplies ``{}``. A previous run may still have
        persisted a removed/stale model; letting that metadata overwrite the
        current empty catalog makes diagnostics lie and can reintroduce
        obsolete selection behavior. Standalone callers that did not
        configure catalogs retain the legacy state round-trip behavior.
        """
        if not registry.has_configured_model_catalog(name):
            status = registry.get_status(name)
            status.models = tuple(models)
            status.selected_model = selected_model

    for name, value in data.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            gate_configured_entry(name, "entry must be an object")
            continue

        # In production the CLI populates the registry from current
        # configuration before loading this cache.  Do not let a stale entry
        # silently re-enable a provider that an operator has since removed.
        # Loading into an empty registry remains supported for standalone
        # callers and backwards compatibility with the original API.
        if restrict_to_configured and name not in configured_names:
            logger.info(
                "ignoring persisted state for unconfigured provider %r in %s",
                name,
                source,
            )
            continue

        try:
            retry_after = (
                datetime.fromisoformat(value["retry_after"])
                if value.get("retry_after")
                else None
            )
        except (TypeError, ValueError):
            gate_configured_entry(
                name,
                "invalid retry_after",
                value.get("checkpoint"),
            )
            continue

        # Older/manual state files may contain an ISO timestamp without a
        # timezone.  The registry clock is UTC-aware, so leaving this naive
        # would make the next ``is_due_for_recheck`` comparison raise and
        # wedge every unattended scheduler tick.  Treat legacy naive values
        # as UTC, matching the format emitted by current versions.
        if retry_after is not None and retry_after.tzinfo is None:
            retry_after = retry_after.replace(tzinfo=timezone.utc)

        checkpoint = value.get("checkpoint")
        if checkpoint is not None and not isinstance(checkpoint, Mapping):
            gate_configured_entry(name, "invalid checkpoint")
            continue
        checkpoint = dict(checkpoint or {})
        capability_limits = value.get("capability_limits")
        if capability_limits is not None and not isinstance(capability_limits, Mapping):
            gate_configured_entry(name, "invalid capability_limits", checkpoint)
            continue
        capability_limits = dict(capability_limits or {})
        has_model_state = "models" in value or "selected_model" in value
        models = value.get("models", [])
        selected_model = value.get("selected_model")
        if (
            not isinstance(models, list)
            or any(not isinstance(model, str) or not model.strip() for model in models)
            or (selected_model is not None and not isinstance(selected_model, str))
            or (selected_model is not None and selected_model not in models)
        ):
            gate_configured_entry(name, "invalid model selection", checkpoint)
            continue

        state = value.get("state")

        if state == ProviderState.LIMITED:
            if retry_after is None:
                # A LIMITED provider without a deadline can never become due
                # for recheck and would therefore remain disabled forever.
                # Older or manually edited state files may omit the field;
                # keep the provider gated, but give the unattended loop a
                # deterministic recovery path.
                logger.warning(
                    "provider state entry %r is LIMITED without retry_after in %s; "
                    "scheduling a fallback recheck",
                    name,
                    source,
                )
                retry_after = _INVALID_STATE_RECHECK_BACKOFF
            registry.mark_limited(
                name,
                retry_after=retry_after,
                checkpoint=checkpoint,
                reason=value.get("last_error"),
            )
            status = registry.get_status(name)
            if has_model_state:
                apply_persisted_model_state(name, models, selected_model)
            status.capability_limits = capability_limits

        elif state == ProviderState.ERROR:
            registry.mark_error(
                name,
                value.get("last_error", ""),
                retry_after=retry_after,
                checkpoint=checkpoint,
            )
            status = registry.get_status(name)
            if has_model_state:
                apply_persisted_model_state(name, models, selected_model)
            status.capability_limits = capability_limits

        elif state == ProviderState.AVAILABLE:
            status = registry.mark_available(name)
            # AVAILABLE does not mean the resume checkpoint is obsolete.
            # A limited provider keeps it when a successful recheck moves
            # back to AVAILABLE, and a process restart must preserve the
            # same progress just as the in-memory transition does.
            status.checkpoint = checkpoint
            if has_model_state:
                apply_persisted_model_state(name, models, selected_model)
            status.capability_limits = capability_limits
        else:
            # Provider state gates paid/external work.  Treating a typo or
            # a value written by an incompatible version as AVAILABLE would
            # fail open and could immediately retry a quota-limited provider.
            logger.warning(
                "provider state entry %r has unknown state %r in %s; "
                "gating provider until recheck",
                name,
                state,
                source,
            )
            registry.mark_error(
                name,
                f"invalid persisted provider state: {state!r}",
                retry_after=_INVALID_STATE_RECHECK_BACKOFF,
                checkpoint=checkpoint,
            )
