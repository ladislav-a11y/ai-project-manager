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
import os
import shlex
from dataclasses import dataclass, field
from typing import Mapping, Optional


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass
class TrelloConfig:
    key: str
    token: str
    board_id: str
    inbox_list_name: str = "Inbox"


@dataclass
class OrchestratorConfig:
    """How to invoke the ai-orchestrator process. ``command`` is the
    argv prefix (e.g. ``["ai-orchestrator"]``); ``--project``, ``--goal``,
    ``--spec`` and ``--agent`` are appended per call (see
    ``orchestrator_runner.build_run_fn``).

    ``project_paths``/``projects_root`` map a project name onto its local
    checkout for ``--project`` - never a single hardcoded path. ``spec_dir``
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
    timeout_seconds: Optional[float] = None
    project_paths: dict = field(default_factory=dict)
    projects_root: Optional[str] = None
    spec_dir: str = "specs"
    outbox_dir: str = "outbox"


@dataclass
class Config:
    trello: TrelloConfig
    orchestrator: OrchestratorConfig
    providers: list
    poll_interval_seconds: float = 300.0
    holder: str = "project-manager"
    providers_for_project: dict = field(default_factory=dict)


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise ConfigError(f"missing required environment variable: {name}")
    return value


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    """Build a ``Config`` from environment variables.

    Recognized variables:
      TRELLO_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID   (required)
      TRELLO_INBOX_LIST                            (default "Inbox")
      AI_ORCHESTRATOR_CMD                          (default "ai-orchestrator")
      AI_ORCHESTRATOR_TIMEOUT_SECONDS              (optional)
      AI_PM_PROJECT_PATHS                          (optional JSON object: project name -> local path)
      AI_PM_PROJECTS_ROOT                          (optional shared base dir for project checkouts)
      AI_ORCHESTRATOR_SPEC_DIR                     (default "specs")
      AI_ORCHESTRATOR_OUTBOX_DIR                   (default "outbox")
      AI_PM_PROVIDERS                              (default "claude")
      AI_PM_PROVIDERS_FOR_PROJECT                  (optional JSON object)
      AI_PM_POLL_INTERVAL_SECONDS                  (default "300")
      AI_PM_HOLDER                                 (default "project-manager")
    """
    env = os.environ if env is None else env

    trello = TrelloConfig(
        key=_require(env, "TRELLO_KEY"),
        token=_require(env, "TRELLO_TOKEN"),
        board_id=_require(env, "TRELLO_BOARD_ID"),
        inbox_list_name=env.get("TRELLO_INBOX_LIST", "Inbox"),
    )

    orchestrator_cmd = env.get("AI_ORCHESTRATOR_CMD", "ai-orchestrator")
    timeout_raw = env.get("AI_ORCHESTRATOR_TIMEOUT_SECONDS")
    try:
        timeout_seconds = float(timeout_raw) if timeout_raw else None
    except ValueError as exc:
        raise ConfigError(
            f"AI_ORCHESTRATOR_TIMEOUT_SECONDS must be a number, got {timeout_raw!r}"
        ) from exc
    project_paths: dict = {}
    raw_project_paths = env.get("AI_PM_PROJECT_PATHS")
    if raw_project_paths:
        try:
            project_paths = json.loads(raw_project_paths)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ConfigError(f"AI_PM_PROJECT_PATHS must be valid JSON: {exc}") from exc

    orchestrator = OrchestratorConfig(
        command=shlex.split(orchestrator_cmd),
        timeout_seconds=timeout_seconds,
        project_paths=project_paths,
        projects_root=env.get("AI_PM_PROJECTS_ROOT"),
        # Resolved to absolute here (not left as a bare relative string)
        # so the outbox result is always read from a fixed, unambiguous
        # directory - never one that silently resolves relative to
        # whatever the Project Manager process's cwd happens to be by
        # the time it actually reads the result back.
        spec_dir=os.path.abspath(env.get("AI_ORCHESTRATOR_SPEC_DIR", "specs")),
        outbox_dir=os.path.abspath(env.get("AI_ORCHESTRATOR_OUTBOX_DIR", "outbox")),
    )

    providers = [p.strip() for p in env.get("AI_PM_PROVIDERS", "claude").split(",") if p.strip()]
    if not providers:
        raise ConfigError("AI_PM_PROVIDERS must list at least one provider")

    providers_for_project: dict = {}
    raw_map = env.get("AI_PM_PROVIDERS_FOR_PROJECT")
    if raw_map:
        try:
            providers_for_project = json.loads(raw_map)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ConfigError(f"AI_PM_PROVIDERS_FOR_PROJECT must be valid JSON: {exc}") from exc

    poll_raw = env.get("AI_PM_POLL_INTERVAL_SECONDS", "300")
    try:
        poll_interval_seconds = float(poll_raw)
    except ValueError as exc:
        raise ConfigError(
            f"AI_PM_POLL_INTERVAL_SECONDS must be a number, got {poll_raw!r}"
        ) from exc

    return Config(
        trello=trello,
        orchestrator=orchestrator,
        providers=providers,
        poll_interval_seconds=poll_interval_seconds,
        holder=env.get("AI_PM_HOLDER", "project-manager"),
        providers_for_project=providers_for_project,
    )
