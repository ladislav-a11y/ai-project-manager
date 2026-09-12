"""Configuration loading for the AI Project Manager entrypoint.

Every secret (Trello credentials) and every environment-specific
setting (which providers exist, how to invoke ai-orchestrator, how
often to poll) comes from the environment - never hardcoded here - so
the same code runs in dev, CI and production by only changing env
vars. Missing required configuration fails loudly (``ConfigError``)
rather than silently falling back to a placeholder secret.
"""

from __future__ import annotations

import json
import math
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _split_command(command: str) -> list:
    """Split an ``AI_ORCHESTRATOR_CMD`` string into argv.

    ``shlex.split``'s default POSIX mode treats backslashes as escape
    characters, which silently mangles a Windows path (e.g.
    ``C:\\Users\\...\\python.exe``) into garbage - every backslash just
    vanishes. On Windows we split in non-POSIX mode instead, which
    leaves backslashes alone; elsewhere POSIX mode (quote/escape
    handling for shell-style strings) is still what we want.
    """
    parts = shlex.split(command, posix=os.name != "nt")
    if os.name == "nt":
        # In non-POSIX mode shlex deliberately preserves surrounding
        # quotes. subprocess with argv, however, expects the executable
        # and arguments without those syntactic quotes.
        parts = [
            part[1:-1]
            if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"}
            else part
            for part in parts
        ]
    if not parts:
        raise ConfigError("AI_ORCHESTRATOR_CMD must contain a command")
    return parts


@dataclass
class TrelloConfig:
    key: str
    token: str
    board_id: str
    inbox_list_name: str = "INBOX / Nápady"


@dataclass
class OrchestratorConfig:
    """How to invoke the ai-orchestrator process. ``command`` is the
    argv prefix (e.g. ``["ai-orchestrator"]``); ``--project``, ``--goal``,
    ``--spec`` and ``--agent`` are appended per call (see
    ``orchestrator_runner.build_run_fn``).

    ``project_paths`` is the explicit project-identity-to-checkout
    allowlist for ``--project``. Keys must match the card's stable
    ``project_key`` label (see ``trello_sync.project_key_from_labels``);
    titles, priority prefixes, descriptions, and slugs are never path
    signals for existing projects. ``projects_root`` is not a fallback for
    an existing ambiguous identity; it is the approved root under which
    Inbox intake may create an isolated path for a genuinely new idea.
    ``spec_dir``
    holds the stable per-project spec file passed as ``--spec``, and
    ``outbox_dir`` is where the result JSON is read back from after a run.

    Both ``spec_dir`` and ``outbox_dir`` are always absolute (resolved at
    load time in ``load_config`` even when a relative default/override was
    given) - ai-orchestrator's outbox is a directory belonging to that
    separate process, not something that should ever be looked up relative
    to wherever the Project Manager process happens to have its own
    working directory.
    """

    command: list
    finalize_command: Optional[list] = None
    allowed_push_remotes: dict = field(default_factory=dict)
    finalize_paths: dict = field(default_factory=dict)
    timeout_seconds: Optional[float] = None
    # v2 Inbox planning budget covers AO's two broker/provider attempts and
    # serialization overhead; the provider slice is derived in cli.py.
    inbox_planner_timeout_seconds: float = 660.0
    project_paths: dict = field(default_factory=dict)
    projects_root: Optional[str] = None
    # Mirrors ai-orchestrator's own config.yaml ``workspace_root`` (the
    # sandbox _ensure_within_workspace enforces). PM has no access to AO's
    # config file, so this must be told to it explicitly - otherwise a new
    # Inbox project can adopt a declared working directory that AO will
    # later refuse at dispatch time, wasting a full implementation cycle to
    # discover what PM could have rejected immediately at intake.
    workspace_root: Optional[str] = None
    # Persisted as a generated checkout's own local git identity right after
    # ``git init`` (see dod_validator.ensure_git_repo_initialized) - without
    # this, AO's later finalization commits (plain ``git commit``, no ``-c``
    # override) fail with "Author identity unknown" the first time real work
    # needs to be committed there (see incident: card P3.01, cw dekoder v1).
    git_user_name: Optional[str] = None
    git_user_email: Optional[str] = None
    spec_dir: str = "specs"
    outbox_dir: str = "outbox"


@dataclass
class Config:
    trello: TrelloConfig
    orchestrator: OrchestratorConfig
    providers: list
    poll_interval_seconds: float = 300.0
    holder: str = "project-manager"
    # Inbox intake remains opt-in until its lifecycle and governance are
    # complete. The production launcher keeps this disabled explicitly.
    inbox_enabled: bool = False
    providers_for_project: dict = field(default_factory=dict)
    provider_models: dict = field(default_factory=dict)
    # One-time migration input for pre-existing production cards that
    # predate the project_key label: Trello card ID *or* exact current
    # card title -> stable project identity (e.g. "AI Project Manager").
    # A real card's title/description routinely has no trace of which
    # project it belongs to (e.g. "P5 - Izolace testovacich Slack
    # notifikaci" never mentions "AI Project Manager"), so there is no
    # content signal left to infer identity from - an explicit,
    # exact-match mapping is the only safe way to seed it without ever
    # guessing (see daemon._bootstrap_project_keys). A title is accepted
    # alongside ID because whoever prepares this map from the Trello UI
    # or a task description only ever sees the card's title, never its
    # internal ID, but only when unique among loaded cards; once applied,
    # the persisted project_key label - not
    # this map - is what survives every later title edit.
    card_project_keys: dict = field(default_factory=dict)
    # Always absolute (resolved at load time, same reasoning as
    # orchestrator.spec_dir/outbox_dir above) so persisted provider
    # state - LIMITED/ERROR, retry_after, in-flight checkpoint - is
    # always read from and written to the same fixed file regardless of
    # whatever working directory the process happens to be started from.
    provider_state_path: str = "provider_state.json"
    # How many unattended blocked-task auto-recovery cycles (see
    # recovery.py) a project may go through before recovery gives up and
    # forces it to a human-required BLOCKED state - the loop guard that
    # keeps a persistently-blocked card from being requeued forever.
    recovery_max_attempts: int = 5
    # Optional explicit root containing disposable pytest basetemp siblings.
    # No root means no autonomous cleanup; this avoids guessing a workspace.
    artifact_cleanup_root: Optional[str] = None
    artifact_cleanup_retention_seconds: float = 86400.0


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value or not value.strip():
        raise ConfigError(f"missing required environment variable: {name}")
    return value


def _non_empty(env: Mapping[str, str], name: str, default: str) -> str:
    """Read a non-secret textual setting and reject blank overrides.

    Environment managers commonly leave a variable defined but empty.  For
    identity-bearing values, silently accepting that is worse than treating it
    as absent: an empty Inbox name can never match a Trello list and an empty
    lock holder makes ownership diagnostics ambiguous.
    """
    value = env.get(name, default)
    if not value or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value


def _absolute_path_setting(
    env: Mapping[str, str], name: str, default: str
) -> str:
    """Resolve a path setting while rejecting an explicitly blank value.

    ``os.path.abspath("")`` resolves to the process working directory. That
    is a surprising and unsafe interpretation for settings that identify a
    state file or a dedicated spec/outbox directory, especially when an
    environment manager has emitted an empty variable.
    """
    return os.path.abspath(_non_empty(env, name, default))


def _boolean_setting(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    """Read a strict boolean environment setting."""
    raw = env.get(name, "1" if default else "0")
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean (0/1, true/false, yes/no, on/off)")


def _load_string_mapping(raw: str, name: str, *, list_values: bool = False) -> dict:
    """Parse a JSON object whose keys and values have a fixed string shape.

    Environment JSON is untrusted configuration.  Merely decoding it is not
    enough: a valid JSON list/string would otherwise survive startup and fail
    much later in path resolution or scheduling with an unrelated TypeError.
    """
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{name} must be valid JSON: {exc}") from exc

    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a JSON object")

    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigError(f"{name} keys must be non-empty strings")
        if list_values:
            if (
                not isinstance(item, list)
                or not item
                or any(not isinstance(entry, str) or not entry.strip() for entry in item)
            ):
                raise ConfigError(f"{name} values must be non-empty lists of non-empty strings")
        elif not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{name} values must be non-empty strings")
    return value


def _default_orchestrator_command(
    env: Mapping[str, str], project_paths: Mapping[str, str]
) -> list[str]:
    """Resolve the v2 AO entrypoint without falling back to PATH aliases."""
    roots: list[Path] = []
    explicit_root = env.get("AI_ORCHESTRATOR_ROOT", "").strip()
    if explicit_root:
        roots.append(Path(explicit_root))
    for key in ("AI Orchestrator", "ai-orchestrator"):
        configured = project_paths.get(key)
        if configured:
            roots.append(Path(configured))
    # The production checkout layout is PM and AO below one workspace root.
    roots.append(Path(__file__).resolve().parents[2] / "ai-orchestrator")

    for root in roots:
        python_exe = root / ".venv" / "Scripts" / "python.exe"
        entrypoint = root / "orchestrator.py"
        if python_exe.is_file() and entrypoint.is_file():
            return [str(python_exe), str(entrypoint), "autonomous", "--no-commit"]

    searched = ", ".join(str(root) for root in roots)
    raise ConfigError(
        "AI_ORCHESTRATOR_CMD není nastaven a v2 AO prostředí nebylo nalezeno. "
        "Nastav AI_ORCHESTRATOR_ROOT nebo AI_ORCHESTRATOR_CMD na AO checkout. "
        f"Ověřené kořeny: {searched}"
    )


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    """Build a ``Config`` from environment variables.

    Recognized variables:
      TRELLO_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID   (required)
      TRELLO_INBOX_LIST                            (default "INBOX / Nápady")
      AI_ORCHESTRATOR_CMD                          (optional; otherwise resolves AO .venv)
      AI_ORCHESTRATOR_ROOT                         (optional AO checkout root)
      AI_ORCHESTRATOR_FINALIZE_CMD                 (optional controller finalize command)
      AI_ORCHESTRATOR_ALLOWED_PUSH_REMOTES         (optional JSON identity -> exact remote URL)
      AI_ORCHESTRATOR_FINALIZE_PATHS               (optional JSON identity -> explicit path list)
      AI_ORCHESTRATOR_TIMEOUT_SECONDS              (optional)
      AI_PM_INBOX_PLANNER_TIMEOUT_SECONDS         (optional, default 660)
      AI_PM_PROJECT_PATHS                          (optional JSON object: project name -> local path)
      AI_PM_PROJECTS_ROOT                          (optional shared base dir for project checkouts)
      AI_ORCHESTRATOR_WORKSPACE_ROOT                (optional; must match AO's config.yaml
                                                     workspace_root so PM can fail closed at
                                                     intake instead of at AO dispatch time)
      AI_PM_GIT_USER_NAME, AI_PM_GIT_USER_EMAIL     (optional; persisted as a generated
                                                     checkout's local git identity so AO's
                                                     later finalization commits succeed)
      AI_ORCHESTRATOR_SPEC_DIR                     (default "specs")
      AI_ORCHESTRATOR_OUTBOX_DIR                   (default "outbox")
      AI_PM_PROVIDERS                              (default "groq,antigravity,claude-code,codex";
                                                     PM metadata only, AO broker selects the provider)
      AI_PM_PROVIDERS_FOR_PROJECT                  (optional JSON object)
      AI_PM_PROVIDER_MODELS                        (optional JSON provider -> ordered model catalog;
                                                     legacy metadata accepted for compatibility;
                                                     production dispatch leaves model routing to AO)
      AI_PM_CARD_PROJECT_KEYS                      (optional JSON object: Trello card ID or exact title -> project identity)
      AI_PM_POLL_INTERVAL_SECONDS                  (default "300")
      AI_PM_HOLDER                                 (default "project-manager")
      AI_PM_ENABLE_INBOX                           (default "0"; opt-in only)
      AI_PM_PROVIDER_STATE_PATH                    (default "provider_state.json")
      AI_PM_RECOVERY_MAX_ATTEMPTS                  (default "5")
    """
    env = os.environ if env is None else env

    inbox_list_name = _non_empty(
        env, "TRELLO_INBOX_LIST", "INBOX / Nápady"
    )
    if inbox_list_name != "INBOX / Nápady":
        raise ConfigError(
            'TRELLO_INBOX_LIST must be exactly "INBOX / Nápady"'
        )

    trello = TrelloConfig(
        key=_require(env, "TRELLO_KEY"),
        token=_require(env, "TRELLO_TOKEN"),
        board_id=_require(env, "TRELLO_BOARD_ID"),
        inbox_list_name=inbox_list_name,
    )

    finalize_cmd = env.get("AI_ORCHESTRATOR_FINALIZE_CMD", "").strip()
    timeout_raw = env.get("AI_ORCHESTRATOR_TIMEOUT_SECONDS")
    try:
        timeout_seconds = float(timeout_raw) if timeout_raw else None
    except ValueError as exc:
        raise ConfigError(
            f"AI_ORCHESTRATOR_TIMEOUT_SECONDS must be a number, got {timeout_raw!r}"
        ) from exc
    if timeout_seconds is not None and (
        not math.isfinite(timeout_seconds) or timeout_seconds <= 0
    ):
        raise ConfigError("AI_ORCHESTRATOR_TIMEOUT_SECONDS must be a finite positive number")
    planner_timeout_raw = env.get("AI_PM_INBOX_PLANNER_TIMEOUT_SECONDS", "660")
    try:
        planner_timeout_seconds = float(planner_timeout_raw)
    except ValueError as exc:
        raise ConfigError(
            "AI_PM_INBOX_PLANNER_TIMEOUT_SECONDS must be a number, "
            f"got {planner_timeout_raw!r}"
        ) from exc
    if not math.isfinite(planner_timeout_seconds) or planner_timeout_seconds <= 0:
        raise ConfigError(
            "AI_PM_INBOX_PLANNER_TIMEOUT_SECONDS must be finite and positive"
        )
    project_paths: dict = {}
    raw_project_paths = env.get("AI_PM_PROJECT_PATHS")
    if raw_project_paths:
        project_paths = _load_string_mapping(raw_project_paths, "AI_PM_PROJECT_PATHS")
    raw_orchestrator_cmd = env.get("AI_ORCHESTRATOR_CMD")
    orchestrator_cmd = raw_orchestrator_cmd.strip() if raw_orchestrator_cmd is not None else ""
    orchestrator_command = (
        _split_command(orchestrator_cmd)
        if raw_orchestrator_cmd is not None
        else _default_orchestrator_command(env, project_paths)
    )
    allowed_push_remotes: dict = {}
    raw_allowed_push_remotes = env.get("AI_ORCHESTRATOR_ALLOWED_PUSH_REMOTES")
    if raw_allowed_push_remotes:
        allowed_push_remotes = _load_string_mapping(
            raw_allowed_push_remotes, "AI_ORCHESTRATOR_ALLOWED_PUSH_REMOTES"
        )
    finalize_paths: dict = {}
    raw_finalize_paths = env.get("AI_ORCHESTRATOR_FINALIZE_PATHS")
    if raw_finalize_paths:
        finalize_paths = _load_string_mapping(
            raw_finalize_paths, "AI_ORCHESTRATOR_FINALIZE_PATHS", list_values=True
        )

    orchestrator = OrchestratorConfig(
        command=orchestrator_command,
        finalize_command=_split_command(finalize_cmd) if finalize_cmd else None,
        allowed_push_remotes=allowed_push_remotes,
        finalize_paths=finalize_paths,
        timeout_seconds=timeout_seconds,
        inbox_planner_timeout_seconds=planner_timeout_seconds,
        project_paths=project_paths,
        projects_root=(
            _absolute_path_setting(env, "AI_PM_PROJECTS_ROOT", "")
            if "AI_PM_PROJECTS_ROOT" in env
            else None
        ),
        workspace_root=(
            _absolute_path_setting(env, "AI_ORCHESTRATOR_WORKSPACE_ROOT", "")
            if "AI_ORCHESTRATOR_WORKSPACE_ROOT" in env
            else None
        ),
        git_user_name=_non_empty(env, "AI_PM_GIT_USER_NAME", "") or None,
        git_user_email=_non_empty(env, "AI_PM_GIT_USER_EMAIL", "") or None,
        # Resolved to absolute here (not left as a bare relative string)
        # so the outbox result is always read from a fixed, unambiguous
        # directory - never one that silently resolves relative to
        # whatever the Project Manager process's cwd happens to be by
        # the time it actually reads the result back.
        spec_dir=_absolute_path_setting(env, "AI_ORCHESTRATOR_SPEC_DIR", "specs"),
        outbox_dir=_absolute_path_setting(env, "AI_ORCHESTRATOR_OUTBOX_DIR", "outbox"),
    )

    providers = [p.strip() for p in env.get("AI_PM_PROVIDERS", "groq,antigravity,claude-code,codex").split(",") if p.strip()]
    if not providers:
        raise ConfigError("AI_PM_PROVIDERS must list at least one provider")
    if len(set(providers)) != len(providers):
        raise ConfigError("AI_PM_PROVIDERS must not contain duplicate provider names")
    if "auto" in {provider.casefold() for provider in providers}:
        raise ConfigError("AI_PM_PROVIDERS nesmí obsahovat alias 'auto'; výběr řídí provider-broker v AO")

    providers_for_project: dict = {}
    raw_map = env.get("AI_PM_PROVIDERS_FOR_PROJECT")
    if raw_map:
        providers_for_project = _load_string_mapping(
            raw_map, "AI_PM_PROVIDERS_FOR_PROJECT", list_values=True
        )
        unknown_providers = sorted(
            {
                provider
                for project_providers in providers_for_project.values()
                for provider in project_providers
                if provider not in providers
            }
        )
        if unknown_providers:
            raise ConfigError(
                "AI_PM_PROVIDERS_FOR_PROJECT references providers not listed in "
                f"AI_PM_PROVIDERS: {', '.join(unknown_providers)}"
            )

    provider_models: dict = {}
    raw_provider_models = env.get("AI_PM_PROVIDER_MODELS")
    if raw_provider_models:
        provider_models = _load_string_mapping(
            raw_provider_models, "AI_PM_PROVIDER_MODELS", list_values=True
        )
        unknown_model_providers = sorted(set(provider_models) - set(providers))
        if unknown_model_providers:
            raise ConfigError(
                "AI_PM_PROVIDER_MODELS references providers not listed in "
                f"AI_PM_PROVIDERS: {', '.join(unknown_model_providers)}"
            )

    card_project_keys: dict = {}
    raw_card_project_keys = env.get("AI_PM_CARD_PROJECT_KEYS")
    if raw_card_project_keys:
        card_project_keys = _load_string_mapping(raw_card_project_keys, "AI_PM_CARD_PROJECT_KEYS")

    poll_raw = env.get("AI_PM_POLL_INTERVAL_SECONDS", "300")
    try:
        poll_interval_seconds = float(poll_raw)
    except ValueError as exc:
        raise ConfigError(
            f"AI_PM_POLL_INTERVAL_SECONDS must be a number, got {poll_raw!r}"
        ) from exc
    if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
        raise ConfigError("AI_PM_POLL_INTERVAL_SECONDS must be a finite positive number")

    recovery_max_attempts_raw = env.get("AI_PM_RECOVERY_MAX_ATTEMPTS", "5")
    try:
        recovery_max_attempts = int(recovery_max_attempts_raw)
    except ValueError as exc:
        raise ConfigError(
            f"AI_PM_RECOVERY_MAX_ATTEMPTS must be an integer, got {recovery_max_attempts_raw!r}"
        ) from exc
    if recovery_max_attempts < 1:
        raise ConfigError("AI_PM_RECOVERY_MAX_ATTEMPTS must be at least 1")

    cleanup_retention_raw = env.get("AI_PM_ARTIFACT_RETENTION_HOURS", "24")
    try:
        cleanup_retention_seconds = float(cleanup_retention_raw) * 3600
    except ValueError as exc:
        raise ConfigError(
            "AI_PM_ARTIFACT_RETENTION_HOURS must be a number, "
            f"got {cleanup_retention_raw!r}"
        ) from exc
    if not math.isfinite(cleanup_retention_seconds) or cleanup_retention_seconds < 0:
        raise ConfigError("AI_PM_ARTIFACT_RETENTION_HOURS must be finite and non-negative")

    return Config(
        trello=trello,
        orchestrator=orchestrator,
        providers=providers,
        poll_interval_seconds=poll_interval_seconds,
        holder=_non_empty(env, "AI_PM_HOLDER", "project-manager"),
        inbox_enabled=_boolean_setting(env, "AI_PM_ENABLE_INBOX"),
        providers_for_project=providers_for_project,
        provider_models=provider_models,
        card_project_keys=card_project_keys,
        provider_state_path=_absolute_path_setting(
            env, "AI_PM_PROVIDER_STATE_PATH", "provider_state.json"
        ),
        recovery_max_attempts=recovery_max_attempts,
        artifact_cleanup_root=(
            _absolute_path_setting(env, "AI_PM_ARTIFACT_CLEANUP_ROOT", "")
            if "AI_PM_ARTIFACT_CLEANUP_ROOT" in env
            else None
        ),
        artifact_cleanup_retention_seconds=cleanup_retention_seconds,
    )
