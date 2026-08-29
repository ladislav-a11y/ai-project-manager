from pathlib import Path
from uuid import uuid4

import pytest


def pytest_configure(config):
    """Keep pytest temporary files inside this checkout on Windows.

    The machine's shared temporary root can be inaccessible to the process,
    and pytest's numbered-directory cleanup then turns otherwise passing
    tests into setup errors. A fresh directory per invocation avoids stale
    cleanup state while still honoring an explicit ``--basetemp`` supplied by
    the caller.
    """
    if config.option.basetemp is None:
        config.option.basetemp = str(
            Path.cwd() / f".pytest-basetemp-{uuid4().hex}"
        )


@pytest.fixture(autouse=True)
def _disable_real_slack_webhook(monkeypatch):
    """Tests must never send notifications to the real Slack webhook."""
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
