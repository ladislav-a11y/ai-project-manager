import os
import requests
import logging

logger = logging.getLogger("ai_project_manager")

_ENABLED_VALUES = {"1", "true", "yes", "on"}


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
        return ""
    usage = result.get("usage")
    total = usage.get("total") if isinstance(usage, dict) else None
    if not isinstance(total, dict):
        return ""

    def value(name: str) -> str:
        raw = total.get(name)
        return str(raw) if raw is not None else "n/a"

    return (
        " | usage: "
        f"input={value('input_tokens')}, output={value('output_tokens')}, "
        f"thinking={value('thinking_tokens')}, total={value('total_tokens')}, "
        f"cost_usd={value('cost_usd')}, source={value('source')}"
    )


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
