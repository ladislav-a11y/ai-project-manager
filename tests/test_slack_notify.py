from ai_project_manager import slack_notify
from datetime import datetime, timezone


class FakeResponse:
    status_code = 200


def test_status_messages_are_timestamped_clear_and_provider_block_is_standalone():
    now = datetime(2026, 8, 31, 16, 30, tzinfo=timezone.utc)

    status = slack_notify.status_message(
        "PM zahajuje práci",
        project="Demo",
        provider="hermes | model: upstage/solar-pro4:free",
        provider_reason=(
            "provider je první dostupný v pořadí hermes, codex; "
            "pro implementaci je vždy použit pevný Hermes Nous free model "
            "`upstage/solar-pro4:free`; platí stejně pro implementaci i audit"
        ),
        now=now,
    )
    blocked = slack_notify.provider_blocked_message(
        "hermes", "2026-08-31T17:00:00+00:00", reason="quota", now=now
    )

    assert status.startswith("[AI status] 2026-08-31T16:30+00:00")
    assert "PM zahajuje práci" in status
    assert "projekt: Demo" in status
    assert "provider: hermes | model: upstage/solar-pro4:free" in status
    assert "proč: provider je první dostupný" in status
    assert "pro implementaci je vždy použit pevný Hermes Nous free model" in status
    assert "upstage/solar-pro4:free" in status
    assert "Provider blokován" in blocked
    assert "blokován do: 2026-08-31T17:00:00+00:00" in blocked
    assert "důvod: quota" in blocked


def test_usage_suffix_reports_unknown_tokens_when_receipt_is_missing():
    assert slack_notify.usage_suffix({}) == (
        " | usage: input=n/a, output=n/a, thinking=n/a, total=n/a, "
        "cost_usd=n/a, source=n/a"
    )


def test_usage_suffix_renders_only_bounded_total_receipt():
    result = {
        "usage": {
            "total": {
                "input_tokens": 84,
                "output_tokens": 22940,
                "thinking_tokens": 12573,
                "total_tokens": 23024,
                "cost_usd": 1.7526974,
                "source": "reported",
            },
            "raw_prompt": "must not appear",
        }
    }

    rendered = slack_notify.usage_suffix(result)

    assert rendered == (
        " | usage: input=84, output=22940, thinking=12573, total=23024, "
        "cost_usd=1.7526974, source=reported"
    )
    assert "raw_prompt" not in rendered


def test_result_model_prefers_actual_receipt_and_supports_usage_model():
    assert slack_notify.result_model({"active_model": "gpt-5.6"}, "configured") == "gpt-5.6"
    assert slack_notify.result_model(
        {"usage": {"total": {"model": "claude-opus-4-1"}}}, "configured"
    ) == "claude-opus-4-1"
    assert slack_notify.result_model({}, "configured") == "configured"


def test_provider_route_detail_makes_internal_failover_visible():
    assert slack_notify.provider_route_detail({
        "provider_sequence": ["hermes", "codex"],
        "active_provider": "codex",
    }) == "provider path: hermes -> codex | failover: ano | model path: hermes=nezjištěn -> codex=nezjištěn"
    assert slack_notify.provider_route_detail({
        "provider_sequence": ["codex"],
        "active_provider": "codex",
    }) == "provider path: codex | failover: ne | model path: codex=nezjištěn"
    assert slack_notify.provider_route_detail(
        {"provider_sequence": ["codex"], "active_provider": "codex"},
        selected_provider="hermes",
    ) == "provider path: hermes -> codex | failover: ano | model path: hermes=nezjištěn -> codex=nezjištěn"


def test_provider_route_detail_shows_model_for_each_provider():
    rendered = slack_notify.provider_route_detail({
        "provider_sequence": ["hermes", "claude-code"],
        "active_provider": "claude-code",
        "active_model": "claude-opus-4-1",
        "usage": {"events": [
            {"provider": "hermes", "model": "upstage/solar-pro4:free"},
            {"provider": "claude-code", "model": "claude-opus-4-1"},
        ]},
    })
    assert "provider path: hermes -> claude-code" in rendered
    assert "model path: hermes=upstage/solar-pro4:free -> claude-code=claude-opus-4-1" in rendered


def test_webhook_url_alone_does_not_enable_real_notifications(monkeypatch, caplog):
    secret_webhook = "https://hooks.slack.invalid/secret"
    caplog.set_level("INFO")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", secret_webhook)
    monkeypatch.delenv("AI_PM_SLACK_ENABLED", raising=False)
    calls = []
    monkeypatch.setattr(slack_notify.requests, "post", lambda *args, **kwargs: calls.append((args, kwargs)))

    slack_notify.notify("local test")

    assert calls == []
    assert "webhook is configured" in caplog.text
    assert "not enabled" in caplog.text
    assert secret_webhook not in caplog.text


def test_explicit_opt_in_sends_notification(monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.invalid/secret")
    monkeypatch.setenv("AI_PM_SLACK_ENABLED", "true")
    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse()

    monkeypatch.setattr(slack_notify.requests, "post", fake_post)
    slack_notify.notify("scheduler started")

    assert calls == [
        (
            ("https://hooks.slack.invalid/secret",),
            {"json": {"text": "scheduler started"}, "timeout": 10},
        )
    ]
    assert "Slack notification delivered (HTTP 200)" in caplog.text


def test_false_like_flag_keeps_notifications_disabled(monkeypatch, caplog):
    secret_webhook = "https://hooks.slack.invalid/secret"
    caplog.set_level("INFO")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", secret_webhook)
    monkeypatch.setenv("AI_PM_SLACK_ENABLED", "0")
    calls = []
    monkeypatch.setattr(slack_notify.requests, "post", lambda *args, **kwargs: calls.append((args, kwargs)))

    slack_notify.notify("must stay local")

    assert calls == []
    assert "not enabled" in caplog.text
    assert secret_webhook not in caplog.text


def test_enabled_without_webhook_logs_safe_reason(monkeypatch, caplog):
    monkeypatch.setenv("AI_PM_SLACK_ENABLED", "yes")
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    calls = []
    monkeypatch.setattr(slack_notify.requests, "post", lambda *args, **kwargs: calls.append((args, kwargs)))

    slack_notify.notify("nothing configured")

    assert calls == []
    assert "AI_PM_SLACK_ENABLED is enabled" in caplog.text
    assert "SLACK_WEBHOOK_URL is missing" in caplog.text


def test_http_failure_logs_status_without_response_or_webhook(monkeypatch, caplog):
    secret_webhook = "https://hooks.slack.invalid/super-secret"
    monkeypatch.setenv("SLACK_WEBHOOK_URL", secret_webhook)
    monkeypatch.setenv("AI_PM_SLACK_ENABLED", "1")

    class FailedResponse:
        status_code = 503
        text = f"upstream echoed credential {secret_webhook}"

    monkeypatch.setattr(slack_notify.requests, "post", lambda *args, **kwargs: FailedResponse())

    slack_notify.notify("scheduler started")

    assert "Slack notification failed with HTTP 503" in caplog.text
    assert secret_webhook not in caplog.text
    assert FailedResponse.text not in caplog.text


def test_transport_error_does_not_leak_webhook_into_logs(monkeypatch, caplog):
    secret_webhook = "https://hooks.slack.invalid/super-secret"
    monkeypatch.setenv("SLACK_WEBHOOK_URL", secret_webhook)
    monkeypatch.setenv("AI_PM_SLACK_ENABLED", "on")

    def fail(*args, **kwargs):
        raise RuntimeError(f"connection failed for {secret_webhook}")

    monkeypatch.setattr(slack_notify.requests, "post", fail)
    slack_notify.notify("scheduler started")

    assert "RuntimeError" in caplog.text
    assert secret_webhook not in caplog.text
