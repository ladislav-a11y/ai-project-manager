import json
import logging
import os
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from ai_project_manager.provider_state import load_provider_state, save_provider_state
from ai_project_manager.providers import ProviderRegistry, ProviderState, detect_limit


def test_detect_limit_accepts_string_status_code_and_http_date_retry_after(monkeypatch):
    class RateLimitError(RuntimeError):
        status_code = "429"

    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("ai_project_manager.providers._utcnow", lambda: now)
    retry_at = now + timedelta(minutes=7)

    delay = detect_limit(
        RateLimitError("temporarily unavailable"),
        headers={"Retry-After": format_datetime(retry_at, usegmt=True)},
    )

    assert delay == timedelta(minutes=7)


def test_detect_limit_clamps_negative_retry_after_to_zero():
    delay = detect_limit(RuntimeError("rate limit"), headers={"Retry-After": "-5"})

    assert delay == timedelta(0)


def test_load_from_missing_file_is_a_safe_no_op(tmp_path):
    registry = ProviderRegistry()

    load_provider_state(tmp_path / "does-not-exist.json", registry)

    assert registry.registered_names() == []


def test_corrupted_state_file_temporarily_gates_registered_providers(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text('{"claude": {"state": "LIMITED"', encoding="utf-8")
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    registry = ProviderRegistry(clock=lambda: now)
    registry.mark_available("claude").checkpoint = {"step": 8}

    load_provider_state(path, registry)

    status = registry.get_status("claude")
    assert status.state == ProviderState.ERROR
    assert status.retry_after == now + timedelta(minutes=5)
    assert status.checkpoint == {"step": 8}
    assert registry.is_available("claude") is False
    assert "gating known providers" in caplog.text


def test_non_object_state_file_temporarily_gates_all_registered_providers(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("[]", encoding="utf-8")
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    registry = ProviderRegistry(clock=lambda: now)
    registry.mark_available("claude")
    registry.mark_available("codex")

    load_provider_state(path, registry)

    for name in ("claude", "codex"):
        status = registry.get_status(name)
        assert status.state == ProviderState.ERROR
        assert status.retry_after == now + timedelta(minutes=5)


def test_save_then_load_round_trips_limited_state_and_checkpoint(tmp_path):
    path = tmp_path / "state.json"
    clock_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    registry = ProviderRegistry(clock=lambda: clock_now)
    registry.mark_limited(
        "claude", retry_after=timedelta(minutes=30), checkpoint={"step": 3}, reason="quota exceeded",
    )

    save_provider_state(path, registry)

    reloaded = ProviderRegistry()
    load_provider_state(path, reloaded)

    status = reloaded.get_status("claude")
    assert status.state == ProviderState.LIMITED
    assert status.retry_after == clock_now + timedelta(minutes=30)
    assert status.checkpoint == {"step": 3}
    assert status.last_error == "quota exceeded"


def test_save_then_load_round_trips_error_state(tmp_path):
    path = tmp_path / "state.json"
    registry = ProviderRegistry()
    registry.mark_error("gpt", "boom", retry_after=timedelta(minutes=5), checkpoint={"step": 1})

    save_provider_state(path, registry)

    reloaded = ProviderRegistry()
    load_provider_state(path, reloaded)

    status = reloaded.get_status("gpt")
    assert status.state == ProviderState.ERROR
    assert status.last_error == "boom"
    assert status.checkpoint == {"step": 1}


def test_save_then_load_round_trips_available_state(tmp_path):
    path = tmp_path / "state.json"
    registry = ProviderRegistry()
    status = registry.mark_available("claude")
    status.checkpoint = {"step": 4, "resume": "tests"}

    save_provider_state(path, registry)

    reloaded = ProviderRegistry()
    load_provider_state(path, reloaded)

    reloaded_status = reloaded.get_status("claude")
    assert reloaded_status.state == ProviderState.AVAILABLE
    assert reloaded_status.checkpoint == {"step": 4, "resume": "tests"}


def test_save_then_load_round_trips_capability_limits(tmp_path):
    path = tmp_path / "state.json"
    registry = ProviderRegistry()
    registry.mark_available("hermes")
    registry.mark_capability_limited(
        "hermes",
        "audit:station agent:propagation a scoring",
        "audit plan without evidence",
    )

    save_provider_state(path, registry)

    reloaded = ProviderRegistry()
    load_provider_state(path, reloaded)

    status = reloaded.get_status("hermes")
    assert status.capability_limits["audit:station agent:propagation a scoring"]["reason"] == (
        "audit plan without evidence"
    )


def test_save_then_load_round_trips_selected_provider_model(tmp_path):
    path = tmp_path / "state.json"
    registry = ProviderRegistry()
    registry.mark_available("codex")
    registry.configure_models("codex", ["gpt-5.6-luna", "gpt-5.4"])

    save_provider_state(path, registry)

    reloaded = ProviderRegistry()
    load_provider_state(path, reloaded)

    status = reloaded.get_status("codex")
    assert status.models == ("gpt-5.6-luna", "gpt-5.4")
    assert status.selected_model == "gpt-5.6-luna"


def test_legacy_provider_state_keeps_models_from_current_configuration(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        '{"codex":{"state":"AVAILABLE","retry_after":null,"checkpoint":{}}}',
        encoding="utf-8",
    )
    registry = ProviderRegistry()
    registry.configure_models("codex", ["gpt-5.6"])

    load_provider_state(path, registry)

    assert registry.selected_model("codex") == "gpt-5.6"


def test_current_empty_model_catalog_does_not_restore_stale_persisted_model(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        '{"antigravity":{"state":"ERROR","retry_after":null,'
        '"checkpoint":{},"models":["gemini-2.5-pro"],'
        '"selected_model":"gemini-2.5-pro"}}',
        encoding="utf-8",
    )
    registry = ProviderRegistry()
    registry.configure_models("antigravity", [])

    load_provider_state(path, registry)

    status = registry.get_status("antigravity")
    assert status.models == ()
    assert status.selected_model is None


def test_save_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "state.json"
    registry = ProviderRegistry()
    registry.mark_available("claude")

    save_provider_state(path, registry)

    assert path.exists()


def test_save_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / "state.json"
    registry = ProviderRegistry()
    registry.mark_available("claude")

    save_provider_state(path, registry)

    assert os.listdir(tmp_path) == ["state.json"]


def test_load_from_corrupted_file_logs_and_starts_fresh_instead_of_crashing(tmp_path, caplog):
    """Regression: a process killed mid-write (crash, power loss, OOM
    kill) used to leave a truncated provider_state.json that crashed
    load_provider_state - and therefore the whole unattended cli.main()
    startup - with an unhandled JSONDecodeError on every subsequent run,
    permanently wedging the autonomous loop until a human deleted the
    file by hand."""
    path = tmp_path / "state.json"
    path.write_text('{"claude": {"state": "LIMITED", "retry_after": null, "last', encoding="utf-8")

    registry = ProviderRegistry()
    with caplog.at_level(logging.WARNING, logger="ai_project_manager"):
        load_provider_state(path, registry)

    assert registry.registered_names() == []
    assert "corrupted" in caplog.text.lower()


def test_load_from_invalid_utf8_logs_and_starts_fresh_instead_of_crashing(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_bytes(b'{"claude": "\xff"}')
    registry = ProviderRegistry()
    registry.mark_available("configured-provider")

    with caplog.at_level(logging.WARNING, logger="ai_project_manager"):
        load_provider_state(path, registry)

    # Loading a disposable corrupt cache must preserve the registry that
    # came from current configuration and let the daemon continue startup.
    assert registry.registered_names() == ["configured-provider"]
    assert "corrupted" in caplog.text.lower()


def test_unreadable_state_file_temporarily_gates_providers_instead_of_crashing(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    registry = ProviderRegistry(clock=lambda: now)
    registry.mark_available("claude").checkpoint = {"step": 8}

    with (
        patch.object(type(path), "read_text", side_effect=PermissionError("locked")),
        caplog.at_level(logging.WARNING, logger="ai_project_manager"),
    ):
        load_provider_state(path, registry)

    status = registry.get_status("claude")
    assert status.state == ProviderState.ERROR
    assert status.retry_after == now + timedelta(minutes=5)
    assert status.checkpoint == {"step": 8}
    assert "gating known providers" in caplog.text


def test_save_after_load_from_corrupted_file_recovers_cleanly(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not json at all", encoding="utf-8")

    registry = ProviderRegistry()
    load_provider_state(path, registry)
    registry.mark_available("claude")
    save_provider_state(path, registry)

    reloaded = ProviderRegistry()
    load_provider_state(path, reloaded)
    assert reloaded.get_status("claude").state == ProviderState.AVAILABLE


def test_load_provider_state_gates_providers_for_non_object_root(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text('["not", "a", "mapping"]', encoding="utf-8")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    with caplog.at_level(logging.WARNING, logger="ai_project_manager"):
        load_provider_state(path, registry)

    assert registry.get_status("claude").state == ProviderState.ERROR
    assert registry.get_status("claude").retry_after is not None
    assert "invalid root" in caplog.text


def test_load_provider_state_skips_invalid_entries_but_loads_valid_ones(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "broken-shape": "not an object",
                "broken-date": {"state": "LIMITED", "retry_after": "tomorrow"},
                "claude": {"state": "AVAILABLE", "retry_after": None},
            }
        ),
        encoding="utf-8",
    )
    registry = ProviderRegistry()

    with caplog.at_level(logging.WARNING, logger="ai_project_manager"):
        load_provider_state(path, registry)

    assert registry.get_status("claude").state == ProviderState.AVAILABLE
    assert registry.registered_names() == ["claude"]
    assert "invalid provider state entry" in caplog.text
    assert "invalid retry_after" in caplog.text


def test_load_provider_state_does_not_restore_removed_provider(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "claude": {"state": "AVAILABLE", "retry_after": None},
                "removed-provider": {
                    "state": "LIMITED",
                    "retry_after": "2026-08-26T20:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    registry = ProviderRegistry()
    registry.mark_available("claude")

    with caplog.at_level(logging.INFO, logger="ai_project_manager"):
        load_provider_state(path, registry)

    assert registry.registered_names() == ["claude"]
    assert "unconfigured provider 'removed-provider'" in caplog.text


def test_load_provider_state_does_not_fail_open_for_unknown_state(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "typo": {"state": "LIMTED", "retry_after": None},
                "missing": {"retry_after": None},
                "claude": {"state": "AVAILABLE", "retry_after": None},
            }
        ),
        encoding="utf-8",
    )
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    registry = ProviderRegistry(clock=lambda: now)
    # This mirrors the production CLI: configured providers are registered
    # AVAILABLE before their persisted states are loaded.
    registry.mark_available("typo")
    registry.mark_available("missing")

    with caplog.at_level(logging.WARNING, logger="ai_project_manager"):
        load_provider_state(path, registry)

    assert registry.get_status("claude").state == ProviderState.AVAILABLE
    for name in ("typo", "missing"):
        status = registry.get_status(name)
        assert status.state == ProviderState.ERROR
        assert status.retry_after == now + timedelta(minutes=5)
        assert registry.is_available(name) is False
    assert "unknown state 'LIMTED'" in caplog.text
    assert "unknown state None" in caplog.text


def test_unknown_state_preserves_checkpoint_for_recovery(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "claude": {
                    "state": "FUTURE_STATE",
                    "retry_after": None,
                    "checkpoint": {"step": 7, "run_id": "abc"},
                }
            }
        ),
        encoding="utf-8",
    )
    registry = ProviderRegistry()

    load_provider_state(path, registry)

    status = registry.get_status("claude")
    assert status.state == ProviderState.ERROR
    assert status.checkpoint == {"step": 7, "run_id": "abc"}


def test_load_provider_state_treats_legacy_naive_retry_after_as_utc(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "claude": {
                    "state": "LIMITED",
                    "retry_after": "2026-08-26T20:00:00",
                    "checkpoint": {"step": 4},
                }
            }
        ),
        encoding="utf-8",
    )
    registry = ProviderRegistry(
        clock=lambda: datetime(2026, 8, 26, 19, 0, tzinfo=timezone.utc)
    )

    load_provider_state(path, registry)

    status = registry.get_status("claude")
    assert status.retry_after == datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)
    assert registry.is_due_for_recheck("claude") is False


def test_load_limited_state_without_retry_after_schedules_recovery(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "claude": {
                    "state": "LIMITED",
                    "retry_after": None,
                    "checkpoint": {"step": 4},
                }
            }
        ),
        encoding="utf-8",
    )
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    registry = ProviderRegistry(clock=lambda: now)

    with caplog.at_level(logging.WARNING, logger="ai_project_manager"):
        load_provider_state(path, registry)

    status = registry.get_status("claude")
    assert status.state == ProviderState.LIMITED
    assert status.retry_after == now + timedelta(minutes=5)
    assert status.checkpoint == {"step": 4}
    assert "without retry_after" in caplog.text


def test_load_provider_state_skips_non_object_checkpoint(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "broken": {
                    "state": "LIMITED",
                    "retry_after": "2026-08-26T20:00:00+00:00",
                    "checkpoint": ["not", "an", "object"],
                },
                "claude": {
                    "state": "AVAILABLE",
                    "retry_after": None,
                    "checkpoint": {},
                },
            }
        ),
        encoding="utf-8",
    )
    registry = ProviderRegistry()

    with caplog.at_level(logging.WARNING, logger="ai_project_manager"):
        load_provider_state(path, registry)

    assert registry.registered_names() == ["claude"]
    assert "invalid checkpoint" in caplog.text


def test_malformed_configured_provider_entry_fails_closed(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "claude": {
                    "state": "LIMITED",
                    "retry_after": "not-a-timestamp",
                    "checkpoint": {"step": 8},
                },
                "codex": {
                    "state": "LIMITED",
                    "retry_after": "2026-08-26T20:00:00+00:00",
                    "checkpoint": ["invalid"],
                },
            }
        ),
        encoding="utf-8",
    )
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    registry = ProviderRegistry(clock=lambda: now)
    registry.mark_available("claude")
    registry.mark_available("codex")

    with caplog.at_level(logging.WARNING, logger="ai_project_manager"):
        load_provider_state(path, registry)

    claude = registry.get_status("claude")
    assert claude.state == ProviderState.ERROR
    assert claude.retry_after == now + timedelta(minutes=5)
    assert claude.checkpoint == {"step": 8}

    codex = registry.get_status("codex")
    assert codex.state == ProviderState.ERROR
    assert codex.retry_after == now + timedelta(minutes=5)
    assert codex.checkpoint == {}
    assert "invalid retry_after" in caplog.text
    assert "invalid checkpoint" in caplog.text


def test_save_is_atomic_target_never_observably_missing_or_truncated(tmp_path):
    path = tmp_path / "state.json"
    registry = ProviderRegistry()
    registry.mark_available("claude")
    save_provider_state(path, registry)

    registry.mark_limited("claude", retry_after=timedelta(minutes=30), checkpoint={"step": 9})
    save_provider_state(path, registry)

    # The file on disk is always one fully-formed JSON document - never
    # a half-written intermediate state - because save_provider_state
    # writes to a temp file first and renames it into place.
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["claude"]["state"] == "LIMITED"
    assert data["claude"]["checkpoint"] == {"step": 9}


def test_save_removes_temporary_file_when_atomic_replace_fails(tmp_path):
    path = tmp_path / "state.json"
    registry = ProviderRegistry()
    registry.mark_available("claude")

    with patch("ai_project_manager.provider_state.os.replace", side_effect=PermissionError):
        try:
            save_provider_state(path, registry)
        except PermissionError:
            pass
        else:
            raise AssertionError("save_provider_state should preserve the replace error")

    assert list(tmp_path.iterdir()) == []
