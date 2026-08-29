import os

import pytest

from ai_project_manager.config import ConfigError, load_config


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_rejects_non_positive_or_non_finite_poll_interval(value):
    env = base_env(AI_PM_POLL_INTERVAL_SECONDS=value)

    with pytest.raises(ConfigError, match="finite positive"):
        load_config(env)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_rejects_non_positive_or_non_finite_orchestrator_timeout(value):
    env = base_env(AI_ORCHESTRATOR_TIMEOUT_SECONDS=value)

    with pytest.raises(ConfigError, match="finite positive"):
        load_config(env)


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


@pytest.mark.parametrize("name", ["TRELLO_KEY", "TRELLO_TOKEN", "TRELLO_BOARD_ID"])
def test_load_config_rejects_whitespace_only_trello_credentials(name):
    with pytest.raises(ConfigError, match=name):
        load_config(base_env(**{name: "   "}))


@pytest.mark.parametrize("name", ["TRELLO_INBOX_LIST", "AI_PM_HOLDER"])
def test_load_config_rejects_blank_identity_settings(name):
    with pytest.raises(ConfigError, match=name):
        load_config(base_env(**{name: "  \t "}))


def test_load_config_defaults_providers_and_orchestrator_command():
    config = load_config(base_env())

    assert config.providers == ["auto"]
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


def test_load_config_preserves_backslashes_in_windows_orchestrator_command(monkeypatch):
    # shlex.split's default POSIX mode treats "\" as an escape character
    # and silently strips it, mangling a Windows path like this one into
    # something that no longer exists on disk. On Windows (os.name ==
    # "nt") the command must be split without POSIX escape handling so
    # the path survives intact.
    monkeypatch.setattr("ai_project_manager.config.os.name", "nt")
    env = base_env(
        AI_ORCHESTRATOR_CMD=r"C:\Users\Admin\AppData\Local\Python\pythoncore-3.14-64\python.exe -m ai_orchestrator",
    )

    config = load_config(env)

    assert config.orchestrator.command == [
        r"C:\Users\Admin\AppData\Local\Python\pythoncore-3.14-64\python.exe",
        "-m",
        "ai_orchestrator",
    ]


def test_load_config_removes_syntactic_quotes_from_windows_command(monkeypatch):
    monkeypatch.setattr("ai_project_manager.config.os.name", "nt")
    env = base_env(
        AI_ORCHESTRATOR_CMD=(
            r'"C:\Program Files\Python\python.exe" -m ai_orchestrator '
            r'--label "two words"'
        ),
    )

    config = load_config(env)

    assert config.orchestrator.command == [
        r"C:\Program Files\Python\python.exe",
        "-m",
        "ai_orchestrator",
        "--label",
        "two words",
    ]


def test_load_config_rejects_whitespace_only_orchestrator_command():
    with pytest.raises(ConfigError, match="AI_ORCHESTRATOR_CMD"):
        load_config(base_env(AI_ORCHESTRATOR_CMD="   "))


def test_load_config_parses_providers_for_project_json():
    env = base_env(
        AI_PM_PROVIDERS="gpt",
        AI_PM_PROVIDERS_FOR_PROJECT='{"Billing": ["gpt"]}',
    )

    config = load_config(env)

    assert config.providers_for_project == {"Billing": ["gpt"]}


def test_load_config_rejects_invalid_providers_for_project_json():
    env = base_env(AI_PM_PROVIDERS_FOR_PROJECT="not json")

    with pytest.raises(ConfigError):
        load_config(env)


@pytest.mark.parametrize(
    "value",
    ['["gpt"]', '"gpt"', '{"Billing": "gpt"}', '{"Billing": []}', '{"Billing": [""]}'],
)
def test_load_config_rejects_invalid_providers_for_project_shape(value):
    with pytest.raises(ConfigError, match="AI_PM_PROVIDERS_FOR_PROJECT"):
        load_config(base_env(AI_PM_PROVIDERS_FOR_PROJECT=value))


def test_load_config_defaults_card_project_keys_to_empty():
    config = load_config(base_env())

    assert config.card_project_keys == {}


def test_load_config_parses_card_project_keys_json():
    env = base_env(
        AI_PM_CARD_PROJECT_KEYS='{"card-42": "AI Project Manager"}',
    )

    config = load_config(env)

    assert config.card_project_keys == {"card-42": "AI Project Manager"}


def test_load_config_rejects_invalid_card_project_keys_json():
    env = base_env(AI_PM_CARD_PROJECT_KEYS="not json")

    with pytest.raises(ConfigError, match="AI_PM_CARD_PROJECT_KEYS"):
        load_config(env)


@pytest.mark.parametrize(
    "value",
    ['["AI Project Manager"]', '"AI Project Manager"', '{"card-42": ["AI Project Manager"]}', '{"card-42": ""}'],
)
def test_load_config_rejects_invalid_card_project_keys_shape(value):
    with pytest.raises(ConfigError, match="AI_PM_CARD_PROJECT_KEYS"):
        load_config(base_env(AI_PM_CARD_PROJECT_KEYS=value))


def test_load_config_rejects_empty_providers_list():
    env = base_env(AI_PM_PROVIDERS="  ,  ")

    with pytest.raises(ConfigError):
        load_config(env)


def test_load_config_rejects_duplicate_provider_names():
    env = base_env(AI_PM_PROVIDERS="claude, codex, claude")

    with pytest.raises(ConfigError, match="duplicate provider names"):
        load_config(env)


def test_load_config_rejects_project_provider_that_is_not_configured():
    env = base_env(
        AI_PM_PROVIDERS="claude,codex",
        AI_PM_PROVIDERS_FOR_PROJECT=(
            '{"Important project": ["claude", "typo-provider"]}'
        ),
    )

    with pytest.raises(ConfigError, match="typo-provider"):
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


def test_load_config_resolves_provider_state_path_to_absolute_by_default():
    config = load_config(base_env())

    # Same reasoning as spec_dir/outbox_dir above: never a bare relative
    # "provider_state.json" that would resolve against whatever cwd the
    # process happens to have when it later loads/saves provider state.
    assert os.path.isabs(config.provider_state_path)
    assert config.provider_state_path == os.path.abspath("provider_state.json")


def test_load_config_honors_custom_provider_state_path():
    env = base_env(AI_PM_PROVIDER_STATE_PATH="/tmp/pm/provider_state.json")

    config = load_config(env)

    assert config.provider_state_path == os.path.abspath("/tmp/pm/provider_state.json")


@pytest.mark.parametrize(
    "name",
    [
        "AI_ORCHESTRATOR_SPEC_DIR",
        "AI_ORCHESTRATOR_OUTBOX_DIR",
        "AI_PM_PROVIDER_STATE_PATH",
    ],
)
def test_load_config_rejects_blank_persistence_paths(name):
    with pytest.raises(ConfigError, match=name):
        load_config(base_env(**{name: "  \t "}))


def test_load_config_parses_project_paths_and_root_and_dirs():
    env = base_env(
        AI_PM_PROJECT_PATHS='{"Dashboard": "/checkouts/dashboard"}',
        AI_PM_PROJECTS_ROOT="/checkouts",
        AI_ORCHESTRATOR_SPEC_DIR="/tmp/specs",
        AI_ORCHESTRATOR_OUTBOX_DIR="/tmp/outbox",
    )

    config = load_config(env)

    assert config.orchestrator.project_paths == {"Dashboard": "/checkouts/dashboard"}
    assert config.orchestrator.projects_root == os.path.abspath("/checkouts")
    # A relative-looking override is still normalized to an absolute path.
    assert config.orchestrator.spec_dir == os.path.abspath("/tmp/specs")
    assert config.orchestrator.outbox_dir == os.path.abspath("/tmp/outbox")


def test_load_config_rejects_invalid_project_paths_json():
    env = base_env(AI_PM_PROJECT_PATHS="not json")

    with pytest.raises(ConfigError):
        load_config(env)


def test_load_config_rejects_blank_projects_root():
    with pytest.raises(ConfigError, match="AI_PM_PROJECTS_ROOT"):
        load_config(base_env(AI_PM_PROJECTS_ROOT="  \t "))


def test_load_config_resolves_relative_projects_root_to_absolute():
    config = load_config(base_env(AI_PM_PROJECTS_ROOT="checkouts"))

    assert config.orchestrator.projects_root == os.path.abspath("checkouts")


@pytest.mark.parametrize(
    "value",
    ['["/checkouts/dashboard"]', '"/checkouts/dashboard"', '{"Dashboard": 42}', '{"Dashboard": ""}'],
)
def test_load_config_rejects_invalid_project_paths_shape(value):
    with pytest.raises(ConfigError, match="AI_PM_PROJECT_PATHS"):
        load_config(base_env(AI_PM_PROJECT_PATHS=value))
