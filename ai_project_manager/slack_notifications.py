"""Best-effort lifecycle notifications for the Project Manager.

Slack is an operator-facing mirror of a durable Trello transition.  A Slack
failure must therefore never change the scheduler result or prevent the
Trello sync from completing.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.request import Request, urlopen

logger = logging.getLogger("ai_project_manager")

SLACK_CHANNEL_ID = "C0BRZ6N7J3Y"
SLACK_API_URL = "https://slack.com/api/chat.postMessage"
LifecycleNotifierFn = Callable[[str, object, Mapping[str, object]], None]


def _default_token_path() -> Path:
    explicit = os.environ.get("AI_PM_SLACK_TOKEN_FILE")
    if explicit:
        return Path(explicit)
    orchestrator_root = os.environ.get("AI_ORCHESTRATOR_ROOT")
    if orchestrator_root:
        return Path(orchestrator_root) / "config" / "slack_bot_token.txt"
    return Path("config") / "slack_bot_token.txt"


def _project_label(project: object) -> str:
    name = str(getattr(project, "name", "unknown project"))
    url = getattr(project, "trello_card_url", None)
    return f"<{url}|{name}>" if url else name


def build_lifecycle_message(
    event: str,
    project: object,
    details: Optional[Mapping[str, object]] = None,
) -> str:
    details = details or {}
    label = _project_label(project)
    status = str(details.get("status") or getattr(getattr(project, "status", None), "value", "unknown"))
    provider = details.get("provider") or getattr(project, "provider", None)
    provider_text = f"; provider: {provider}" if provider else ""

    if event == "work_started":
        return f"PM: začíná práce na kartě {label} (workflow: Pracuje se{provider_text})"
    if event == "audit_started":
        return f"PM: začíná audit karty {label} (workflow: Testování{provider_text})"
    if event == "audit_finished":
        result = str(details.get("result") or "unknown").upper()
        reason = details.get("reason")
        suffix = f" — {reason}" if reason else ""
        return f"PM: audit ukončen — výsledek: {result} — karta {label} (stav: {status}{suffix})"
    if event == "workflow_transition":
        previous = details.get("from_status") or details.get("from") or "unknown"
        destination = details.get("to") or status
        return f"PM: karta přesunuta ve workflow — {label}: {previous} → {destination}{provider_text}"
    if event == "finalization_blocked":
        return (
            f"PM: controller finalizace zablokována — {label} "
            f"(stav: {status}): {details.get('reason') or 'neznámý důvod'}"
        )
    if event == "human_required":
        return f"PM: karta čeká na člověka — {label}: {details.get('reason') or 'vyžadována lidská akce'}"
    if event == "human_block_cleared":
        return f"PM: lidská blokace odstraněna — {label}; pokračování ve workflow (stav: {status})"
    if event == "recovery_requeued":
        return f"PM: karta znovu zařazena po recovery — {label} (stav: {status})"
    return f"PM: workflow událost {event} — {label} (stav: {status})"


class SlackLifecycleNotifier:
    """Post lifecycle messages using the existing AO Slack bot token."""

    def __init__(
        self,
        token_path: Optional[str | Path] = None,
        *,
        channel_id: str = SLACK_CHANNEL_ID,
        post_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.token_path = Path(token_path) if token_path else _default_token_path()
        self.channel_id = channel_id
        self._post_fn = post_fn

    def __call__(self, event: str, project: object, details: Mapping[str, object]) -> None:
        message = build_lifecycle_message(event, project, details)
        if self._post_fn is not None:
            self._post_fn(message)
            return
        try:
            token = self.token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("Slack lifecycle notification skipped; token unavailable: %s", exc)
            return
        if not token:
            logger.warning("Slack lifecycle notification skipped; token file is empty: %s", self.token_path)
            return
        payload = json.dumps({"channel": self.channel_id, "text": message}).encode("utf-8")
        request = Request(
            SLACK_API_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed Slack endpoint
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            raise RuntimeError(f"Slack API rejected lifecycle notification: {body.get('error', 'unknown error')}")


def emit_lifecycle(
    notifier: Optional[LifecycleNotifierFn],
    event: str,
    project: object,
    **details: object,
) -> None:
    """Call an injected notifier without allowing Slack to affect PM."""
    if notifier is None:
        return
    try:
        notifier(event, project, details)
    except Exception:  # noqa: BLE001 - operator notification is non-critical
        logger.exception("Slack lifecycle notification failed: event=%s project=%s", event, getattr(project, "name", "?"))
