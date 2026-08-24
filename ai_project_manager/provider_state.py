"""Persistent storage for AI provider availability state."""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime

from .providers import ProviderRegistry, ProviderState


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
        }

    target.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )


def load_provider_state(path: str | Path, registry: ProviderRegistry) -> None:
    source = Path(path)

    if not source.exists():
        return

    data = json.loads(source.read_text(encoding="utf-8"))

    for name, value in data.items():
        retry_after = (
            datetime.fromisoformat(value["retry_after"])
            if value.get("retry_after")
            else None
        )

        if value.get("state") == ProviderState.LIMITED:
            registry.mark_limited(
                name,
                retry_after=retry_after,
                checkpoint=value.get("checkpoint"),
                reason=value.get("last_error"),
            )

        elif value.get("state") == ProviderState.ERROR:
            registry.mark_error(
                name,
                value.get("last_error", ""),
                retry_after=retry_after,
                checkpoint=value.get("checkpoint"),
            )

        else:
            registry.mark_available(name)