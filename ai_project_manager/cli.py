"""Command-line entrypoint: ``python -m ai_project_manager`` or the
``ai-project-manager`` console script.

All configuration (Trello credentials, providers, how to invoke
ai-orchestrator, poll interval) comes from the environment via
``config.load_config`` - nothing here is hardcoded. ``--once`` runs a
single scheduler tick and exits, a safe way to live-test the whole
wiring; without it the process loops forever, which is the normal
unattended mode.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional, Sequence

from .config import ConfigError, load_config
from .daemon import run_loop, run_maintenance_only
from .orchestrator_runner import (
    build_audit_run_fn,
    build_finalize_fn,
    build_inbox_planner_fn,
    build_run_fn,
)
from .providers import ProviderRegistry
from .provider_state import load_provider_state
from .scheduler import AUTO_PROVIDER_ORDER
from .self_update import RESTART_REQUIRED_EXIT_CODE
from .slack_notify import notify, status_message
from .trello_client import RealTrelloClient

logger = logging.getLogger("ai_project_manager")

_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


def _log_level(value: str) -> str:
    level = value.upper()
    if level not in _LOG_LEVELS:
        raise argparse.ArgumentTypeError(
            f"invalid log level {value!r}; choose from {', '.join(_LOG_LEVELS)}"
        )
    return level


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-project-manager")
    parser.add_argument(
        "--slack-probe",
        action="store_true",
        help=(
            "send one production Slack delivery probe and exit; does not load "
            "Trello, dispatch work, or start the scheduler"
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single scheduler tick and exit, instead of looping forever",
    )
    parser.add_argument(
        "--maintain-only",
        action="store_true",
        help=(
            "run one live Trello Card Contract migration/cleanup pass and exit - "
            "never dispatches a project or touches provider state; the scheduler "
            "itself stays on HOLD"
        ),
    )
    parser.add_argument(
        "--enable-inbox-intake",
        action="store_true",
        help="explicitly enable governed processing of the main-board Inbox",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        type=_log_level,
        metavar="LEVEL",
        help="logging level: CRITICAL, ERROR, WARNING, INFO, or DEBUG (default: INFO)",
    )
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    client=None,
    run_fn=None,
    audit_run_fn=None,
    finalize_fn=None,
) -> int:
    """Entrypoint. ``client``/``run_fn``/``audit_run_fn``/``finalize_fn`` are
    only ever passed by tests to inject an in-memory Trello client / fake
    orchestrator dispatch and exercise the real config -> registry ->
    scheduler-loop wiring without a network call; production use (the
    console script / ``python -m ai_project_manager``) always leaves them
    unset and gets the real ``RealTrelloClient`` + ``build_run_fn`` /
    ``build_audit_run_fn`` / ``build_finalize_fn`` built from
    ``load_config()``.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.slack_probe:
        logger.info("starting isolated Slack delivery probe")
        delivered = notify("AI Project Manager: fresh-shell production Slack probe")
        return 0 if delivered else 1

    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        return 2

    if client is None:
        client = RealTrelloClient(
            key=config.trello.key,
            token=config.trello.token,
            board_id=config.trello.board_id,
        )

    if args.maintain_only:
        issues = run_maintenance_only(
            client,
            project_paths=config.orchestrator.project_paths,
            card_project_keys=config.card_project_keys,
            projects_root=config.orchestrator.projects_root,
        )
        logger.info("maintenance-only pass complete: %d issue(s)", len(issues))
        return 1 if issues else 0

    provider_registry = ProviderRegistry()
    registry_names = []
    for name in config.providers:
        if name.casefold() == "auto":
            # ``auto`` is only a routing alias. Register the concrete PM
            # identities so a restart cannot lose provider availability or
            # retry state after a tick that used the alias.
            names = [candidate for candidate in AUTO_PROVIDER_ORDER if candidate != "claude-code"]
        else:
            names = [name]
        for candidate in names:
            if candidate not in registry_names:
                registry_names.append(candidate)
    for name in registry_names:
        provider_registry.mark_available(name)
        provider_registry.configure_models(name, config.provider_models.get(name, []))

    load_provider_state(config.provider_state_path, provider_registry)

    validate_repository_paths = run_fn is None
    if run_fn is None:
        run_fn = build_run_fn(
            provider_registry,
            command=config.orchestrator.command,
            project_paths=config.orchestrator.project_paths,
            projects_root=config.orchestrator.projects_root,
            spec_dir=config.orchestrator.spec_dir,
            outbox_dir=config.orchestrator.outbox_dir,
            timeout_seconds=config.orchestrator.timeout_seconds,
            finalize_command=config.orchestrator.finalize_command,
            finalize_paths=config.orchestrator.finalize_paths,
            allowed_push_remotes=config.orchestrator.allowed_push_remotes,
            use_provider_failover=True,
        )

    if audit_run_fn is None:
        audit_run_fn = build_audit_run_fn(
            provider_registry,
            command=config.orchestrator.command,
            project_paths=config.orchestrator.project_paths,
            projects_root=config.orchestrator.projects_root,
            spec_dir=config.orchestrator.spec_dir,
            outbox_dir=config.orchestrator.outbox_dir,
            timeout_seconds=config.orchestrator.timeout_seconds,
            use_provider_failover=True,
        )

    if finalize_fn is None:
        finalize_fn = build_finalize_fn(
            command=config.orchestrator.finalize_command,
            project_paths=config.orchestrator.project_paths,
            projects_root=config.orchestrator.projects_root,
            finalize_paths=config.orchestrator.finalize_paths,
            allowed_push_remotes=config.orchestrator.allowed_push_remotes,
            timeout_seconds=config.orchestrator.timeout_seconds,
        )

    def notify_inbox_selection(selection: dict) -> None:
        """Expose provider/model selection before the read-only planner call."""
        notify(status_message(
            "PM zahajuje Inbox intake",
            project=selection.get("source_card_name"),
            provider=(
                f"{selection.get('provider')} | "
                f"model: {selection.get('model') or 'n/a'}"
            ),
            provider_reason=(
                f"{selection.get('provider_reason')}; "
                f"{selection.get('model_reason')}"
            ),
            detail=f"source_card_id={selection.get('source_card_id')}; task_type={selection.get('task_type')}",
        ))

    inbox_planner = build_inbox_planner_fn(
        provider_registry,
        command=config.orchestrator.command,
        project_paths=config.orchestrator.project_paths,
        timeout_seconds=180,
        selection_notifier=notify_inbox_selection,
    )

    logger.info(
        "starting ai-project-manager (once=%s, providers=%s, poll_interval=%ss, inbox_enabled=%s, inbox_list=%r)",
        args.once,
        config.providers,
        config.poll_interval_seconds,
        config.inbox_enabled or args.enable_inbox_intake,
        config.trello.inbox_list_name,
    )

    outcome = run_loop(
        client,
        provider_registry,
        run_fn,
        once=args.once,
        poll_interval_seconds=config.poll_interval_seconds,
        holder=config.holder,
        providers_for_project=config.providers_for_project,
        default_providers=config.providers,
        inbox_list_name=config.trello.inbox_list_name,
        process_inbox_enabled=config.inbox_enabled or args.enable_inbox_intake,
        provider_state_path=config.provider_state_path,
        project_paths=(
            config.orchestrator.project_paths if validate_repository_paths else None
        ),
        projects_root=(
            config.orchestrator.projects_root if validate_repository_paths else None
        ),
        card_project_keys=config.card_project_keys,
        recovery_max_attempts=config.recovery_max_attempts,
        audit_run_fn=audit_run_fn,
        inbox_planner=inbox_planner,
        artifact_cleanup_root=config.artifact_cleanup_root,
        artifact_cleanup_retention_seconds=config.artifact_cleanup_retention_seconds,
        finalize_fn=finalize_fn,
    )

    if outcome.restart_required:
        # Never restart in-process - the whole point is that this
        # process's already-imported modules are stale. Exit with a
        # dedicated code so the supervising watchdog (watchdog.py), a
        # separate parent process, is the only thing that ever launches
        # the replacement.
        logger.warning("exiting for supervised restart: %s", outcome.reason)
        return RESTART_REQUIRED_EXIT_CODE

    if args.once:
        logger.info("--once tick complete: ran=%s reason=%s", outcome.ran, outcome.reason)
        if outcome.operational_error:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
