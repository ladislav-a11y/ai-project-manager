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
from .daemon import run_loop
from .orchestrator_runner import build_run_fn
from .providers import ProviderRegistry
from .provider_state import load_provider_state
from .trello_client import RealTrelloClient

logger = logging.getLogger("ai_project_manager")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-project-manager")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single scheduler tick and exit, instead of looping forever",
    )
    parser.add_argument("--log-level", default="INFO", help="logging level (default: INFO)")
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    client=None,
    run_fn=None,
) -> int:
    """Entrypoint. ``client``/``run_fn`` are only ever passed by tests to
    inject an in-memory Trello client / fake orchestrator dispatch and
    exercise the real config -> registry -> scheduler-loop wiring without
    a network call; production use (the console script / ``python -m
    ai_project_manager``) always leaves them unset and gets the real
    ``RealTrelloClient`` + ``build_run_fn`` built from ``load_config()``.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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

    provider_registry = ProviderRegistry()
    for name in config.providers:
        provider_registry.mark_available(name)

    load_provider_state("provider_state.json", provider_registry)

    if run_fn is None:
        run_fn = build_run_fn(
            provider_registry,
            command=config.orchestrator.command,
            project_paths=config.orchestrator.project_paths,
            projects_root=config.orchestrator.projects_root,
            spec_dir=config.orchestrator.spec_dir,
            outbox_dir=config.orchestrator.outbox_dir,
        )

    logger.info(
        "starting ai-project-manager (once=%s, providers=%s, poll_interval=%ss)",
        args.once, config.providers, config.poll_interval_seconds,
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
    )

    if args.once:
        logger.info("--once tick complete: ran=%s reason=%s", outcome.ran, outcome.reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
