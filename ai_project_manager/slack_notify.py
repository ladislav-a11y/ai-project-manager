import os
import requests
import logging

logger = logging.getLogger("ai_project_manager")


def notify(message: str) -> None:
    """
    Send notification to Slack if webhook is configured.
    Never breaks the main scheduler.
    """
    webhook = os.environ.get("SLACK_WEBHOOK_URL")

    if not webhook:
        return

    try:
        response = requests.post(
            webhook,
            json={"text": message},
            timeout=10,
        )

        if response.status_code != 200:
            logger.warning(
                "Slack notification failed: %s %s",
                response.status_code,
                response.text,
            )

    except Exception as exc:
        logger.warning("Slack notification error: %s", exc)