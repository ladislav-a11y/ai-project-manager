"""Regression guard for the 2026-08-26 incident: running the "safe"
scripts/verify_once_resolves_project_path.py verification script must never
post to the real Slack webhook, even if the invoking shell still carries
AI_PM_SLACK_ENABLED / SLACK_WEBHOOK_URL left over from a previous production
scripts/run-ai-project-manager.ps1 run.
"""

import importlib.util
import sys
import tempfile
from pathlib import Path

from ai_project_manager import slack_notify

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "verify_once_resolves_project_path.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("verify_once_resolves_project_path", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verify_script_clears_inherited_slack_env_before_running(monkeypatch):
    # Simulate a shell that still has production Slack notifications enabled
    # from an earlier real --once run via run-ai-project-manager.ps1.
    monkeypatch.setenv("AI_PM_SLACK_ENABLED", "1")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.invalid/leaked-production-webhook")

    calls = []
    monkeypatch.setattr(slack_notify.requests, "post", lambda *a, **k: calls.append((a, k)))

    module = _load_script_module()

    with tempfile.TemporaryDirectory(prefix="verify-once-test-") as tmp:
        workdir = Path(tmp)
        result = module.run_one(workdir, "P5 - Station Agent", str(workdir / "station-agent-checkout"))

    # The stub completes the implementation DoD, so the PM deliberately
    # leaves the card in Testování for the independent ai-orchestrator audit.
    assert result.startswith("OK")
    assert calls == []
    assert "AI_PM_SLACK_ENABLED" not in module.os.environ
    assert "SLACK_WEBHOOK_URL" not in module.os.environ
