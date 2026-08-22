import os

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


def test_load_config_defaults_orchestrator_project_and_outbox_paths():
    config = load_config(base_env())

    assert config.orchestrator.project_paths == {}
    assert config.orchestrator.projects_root is None
    # Always absolute - never a bare relative "specs"/"outbox" that would
    # resolve against whatever the process's cwd happens to be later.
    assert os.path.isabs(config.orchestrator.spec_dir)
    assert config.orchestrator.spec_dir == os.path.abspath("specs")
    assert os.path.isabs(config.orchestrator.outbox_dir)
    assert config.orchestrator.outbox_dir == os.path.abspath("outbox")


def test_load_config_parses_project_paths_and_root_and_dirs():
    env = base_env(
        AI_PM_PROJECT_PATHS='{"Dashboard": "/checkouts/dashboard"}',
        AI_PM_PROJECTS_ROOT="/checkouts",
        AI_ORCHESTRATOR_SPEC_DIR="/tmp/specs",
        AI_ORCHESTRATOR_OUTBOX_DIR="/tmp/outbox",
    )

    config = load_config(env)

    assert config.orchestrator.project_paths == {"Dashboard": "/checkouts/dashboard"}
    assert config.orchestrator.projects_root == "/checkouts"
    # A relative-looking override is still normalized to an absolute path.
    assert config.orchestrator.spec_dir == os.path.abspath("/tmp/specs")
    assert config.orchestrator.outbox_dir == os.path.abspath("/tmp/outbox")


def test_load_config_rejects_invalid_project_paths_json():
    env = base_env(AI_PM_PROJECT_PATHS="not json")

    with pytest.raises(ConfigError):
        load_config(env)
