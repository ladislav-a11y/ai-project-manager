import os
import requests
import logging
from datetime import datetime

logger = logging.getLogger("ai_project_manager")

_ENABLED_VALUES = {"1", "true", "yes", "on"}


def status_message(
    action: str,
    *,
    project: str | None = None,
    provider: str | None = None,
    provider_reason: str | None = None,
    detail: str | None = None,
    now: datetime | None = None,
) -> str:
    """Build one compact, consistently timestamped operational status."""
    timestamp = (now or datetime.now().astimezone()).isoformat(timespec="minutes")
    parts = [f"[AI status] {timestamp}", action]
    if project:
        parts.append(f"projekt: {project}")
    if provider:
        parts.append(f"provider: {provider}")
    if provider_reason:
        parts.append(f"proč: {provider_reason}")
    if detail:
        parts.append(detail)
    return " | ".join(parts)


def provider_blocked_message(
    provider: str,
    retry_after: str,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> str:
    """Render provider backoff as a standalone AI-status message."""
    detail = f"blokován do: {retry_after}"
    if reason:
        detail += f" | důvod: {reason}"
    return status_message(
        "Provider blokován",
        provider=provider,
        detail=detail,
        now=now,
    )


def _notifications_enabled() -> bool:
    """Require an explicit opt-in before contacting a Slack webhook."""
    return os.environ.get("AI_PM_SLACK_ENABLED", "").strip().lower() in _ENABLED_VALUES


def usage_suffix(result: dict | None) -> str:
    """Render the bounded provider usage receipt for an operational message.

    ai-orchestrator keeps usage under ``usage.total``.  Preserve unknown or
    missing values as ``n/a`` and never include prompts, credentials, or raw
    provider output in Slack.
    """
    if not isinstance(result, dict):
        result = {}
    usage = result.get("usage")
    total = usage.get("total") if isinstance(usage, dict) else None
    if not isinstance(total, dict):
        total = {}

    def value(name: str) -> str:
        raw = total.get(name)
        return str(raw) if raw is not None else "n/a"

    return (
        " | usage: "
        f"input={value('input_tokens')}, output={value('output_tokens')}, "
        f"thinking={value('thinking_tokens')}, total={value('total_tokens')}, "
        f"cost_usd={value('cost_usd')}, source={value('source')}"
    )


def provider_route_detail(
    result: dict | None,
    *,
    selected_provider: str | None = None,
) -> str:
    """Render the provider route so failover is visible in Slack.

    The selected provider and the provider that finished a run are not always
    the same: ai-orchestrator may fail over internally.  A final Slack status
    must expose that route without including prompts or raw provider output.
    """
    if not isinstance(result, dict):
        return "provider path: n/a | failover: n/a"
    sequence = result.get("provider_sequence")
    if not isinstance(sequence, list):
        sequence = []
    names = [name.strip() for name in sequence if isinstance(name, str) and name.strip()]
    selected_text = (
        str(selected_provider).strip()
        if isinstance(selected_provider, str) and selected_provider.strip()
        else None
    )
    active = result.get("active_provider")
    active_text = str(active).strip() if isinstance(active, str) and active.strip() else None
    if selected_text and selected_text not in names:
        names.insert(0, selected_text)
    if not names and active_text:
        names = [active_text]
    if not names:
        return "provider path: n/a | failover: n/a"
    failed_over = len(names) > 1
    detail = f"provider path: {' -> '.join(names)} | failover: {'ano' if failed_over else 'ne'}"
    if active_text and active_text != names[-1]:
        detail += f" | aktivní provider: {active_text}"
    return detail


def result_model(result: dict | None, fallback: str | None = None) -> str | None:
    """Return the bounded model identity confirmed by an orchestrator receipt."""
    if not isinstance(result, dict):
        return fallback
    direct = result.get("active_model") or result.get("model")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    usage = result.get("usage")
    total = usage.get("total") if isinstance(usage, dict) else None
    model = total.get("model") if isinstance(total, dict) else None
    return model.strip() if isinstance(model, str) and model.strip() else fallback


def notify(message: str) -> bool:
    """
    Send notification when Slack is explicitly enabled and configured.
    Never breaks the main scheduler.  The boolean return is a secret-free
    delivery receipt for explicit operational probes; existing scheduler
    callers may safely ignore it.
    """
    # A webhook URL is a credential, not an instruction to emit messages.
    # Developer shells and test runners can inherit the production URL, so
    # keep enablement separate to prevent local runs from notifying Slack.
    webhook = os.environ.get("SLACK_WEBHOOK_URL")

    if not _notifications_enabled():
        if webhook:
            logger.info(
                "Slack notification skipped: webhook is configured but "
                "AI_PM_SLACK_ENABLED is not enabled"
            )
        return False

    if not webhook:
        logger.warning(
            "Slack notification skipped: AI_PM_SLACK_ENABLED is enabled but "
            "SLACK_WEBHOOK_URL is missing"
        )
        return False

    try:
        response = requests.post(
            webhook,
            json={"text": message},
            timeout=10,
        )

        if response.status_code != 200:
            logger.warning("Slack notification failed with HTTP %s", response.status_code)
            return False
        else:
            # This secret-free receipt is the operational proof consumed by
            # scripts/verify-scheduler.ps1. Merely having a configured
            # webhook must never be reported as successful delivery.
            logger.info("Slack notification delivered (HTTP 200)")
            return True

    except Exception as exc:
        # requests exceptions can contain their prepared URL, which is the
        # Slack credential itself.  Log only the exception class.
        logger.warning("Slack notification error (%s)", type(exc).__name__)
        return False
