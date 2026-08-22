import pytest

from ai_project_manager.config import ConfigError, load_config


def base_env(**overrides):
    env = {
        "TRELLO_KEY": "trello-key",
        "TRELLO_TOKEN": "trello-token",
        "TRELLO_BOARD_ID": "board-123",
    }
    env.update(overrides)
    return env


def test_load_config_reads_trello_credentials_from_env_without_hardcoding():
    config = load_config(base_env())

    assert config.trello.key == "trello-key"
    assert config.trello.token == "trello-token"
    assert config.trello.board_id == "board-123"
    assert config.trello.inbox_list_name == "Inbox"


def test_load_config_missing_trello_credential_raises_config_error():
    env = base_env()
    del env["TRELLO_TOKEN"]

    with pytest.raises(ConfigError):
        load_config(env)


def test_load_config_defaults_providers_and_orchestrator_command():
    config = load_config(base_env())

    assert config.providers == ["claude"]
    assert config.orchestrator.command == ["ai-orchestrator"]
    assert config.poll_interval_seconds == 300.0
    assert config.holder == "project-manager"


def test_load_config_parses_custom_providers_and_orchestrator_command():
    env = base_env(
        AI_PM_PROVIDERS="claude, gpt , gemini",
        AI_ORCHESTRATOR_CMD="python -m ai_orchestrator run --mode autonomous",
        AI_PM_POLL_INTERVAL_SECONDS="45",
        AI_PM_HOLDER="worker-1",
        TRELLO_INBOX_LIST="Intake",
    )

    config = load_config(env)

    assert config.providers == ["claude", "gpt", "gemini"]
    assert config.orchestrator.command == ["python", "-m", "ai_orchestrator", "run", "--mode", "autonomous"]
    assert config.poll_interval_seconds == 45.0
    assert config.holder == "worker-1"
    assert config.trello.inbox_list_name == "Intake"


def test_load_config_parses_providers_for_project_json():
    env = base_env(AI_PM_PROVIDERS_FOR_PROJECT='{"Billing": ["gpt"]}')

    config = load_config(env)

    assert config.providers_for_project == {"Billing": ["gpt"]}


def test_load_config_rejects_invalid_providers_for_project_json():
    env = base_env(AI_PM_PROVIDERS_FOR_PROJECT="not json")

    with pytest.raises(ConfigError):
        load_config(env)


def test_load_config_rejects_empty_providers_list():
    env = base_env(AI_PM_PROVIDERS="  ,  ")

    with pytest.raises(ConfigError):
        load_config(env)


def test_load_config_rejects_non_numeric_poll_interval():
    env = base_env(AI_PM_POLL_INTERVAL_SECONDS="soon")

    with pytest.raises(ConfigError):
        load_config(env)
