"""Safe, standalone proof that a real ``--once`` tick resolves a Trello
card's local checkout path via identity matching alone - never via the
card's exact current title.

Runs the real production entrypoint (``ai_project_manager.cli.main``)
through its actual ``load_config`` -> ``build_run_fn`` ->
``resolve_project_path`` -> ``subprocess.run`` -> outbox read-back wiring,
the same code path a live ``--once`` invocation takes. Only two things are
swapped for safe local stand-ins, exactly as the test suite already does:

  - the Trello client: ``InMemoryTrelloClient`` instead of the real API,
    so this never makes a network call or touches the live board;
  - the ai-orchestrator executable: a tiny local stub instead of the real
    autonomous coding agent, so this never spawns a real agent run.

``AI_PM_PROJECT_PATHS`` below is intentionally keyed only by each
project's stable identity phrase (e.g. "Station Agent"), never by any
card's exact current title - mirroring the real
``scripts/run-ai-project-manager.ps1`` production config after the fix.
The card titles used are the real, currently-failing shapes reported in
the project goal (Station Agent work cards, a "revize MD/JSON" card),
each with a different P0-P5 prefix and differently worded suffix.

Usage: ``python scripts/verify_once_resolves_project_path.py``
Exits non-zero and prints which card failed if any resolution regresses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_project_manager.cli import main  # noqa: E402
from ai_project_manager.models import ProjectRecord, ProjectStatus  # noqa: E402
from ai_project_manager.trello_client import InMemoryTrelloClient  # noqa: E402
from ai_project_manager.trello_sync import (  # noqa: E402
    build_list_maps,
    project_from_card,
    sync_project_to_trello,
)

CARD_TITLES = [
    # Deliberately excludes any "(čeká" title: scheduler.is_schedulable()
    # treats that as a status/checkpoint note, not executable work, by
    # design - unrelated to path resolution, so it would never reach
    # resolve_project_path() at all.
    "P5 - Station Agent",
    "P4 — Station Agent checkpoint",
    "P0 — Station Agent: revize MD/JSON",
    "P2 Station Agent",
]

# The real reported failure this fix's root cause targets: a generic work
# card whose title never mentions the project it belongs to at all (no
# "Station Agent"/"AI Project Manager" phrase anywhere), only resolvable
# via its project_key label - never via title-phrase matching.
LABEL_ONLY_CARD_TITLE = "P5 — Izolace testovacích Slack notifikací"

STUB_SOURCE = """import json, sys
from pathlib import Path
outbox_dir = Path(sys.argv[1])
opts = {}
it = iter(sys.argv[2:])
for token in it:
    opts[token] = next(it, None)
outbox_dir.mkdir(parents=True, exist_ok=True)
payload = {
    "status": "completed",
    "checkpoint": {"completed_dod_indices": [0]},
    "run_id": opts["--run-id"],
    "last_output": "resolved project path: " + opts["--project"],
}
(outbox_dir / ("autonomous-" + opts["--run-id"] + ".json")).write_text(
    json.dumps(payload), encoding="utf-8"
)
"""


def _ensure_clean_git_checkout(path: str) -> None:
    checkout = Path(path)
    checkout.mkdir(parents=True, exist_ok=True)
    if (checkout / ".git").is_dir():
        return
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.email", "verify@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.name", "PM verifier"], check=True)
    subprocess.run(["git", "-C", str(checkout), "commit", "--allow-empty", "-q", "-m", "initial"], check=True)


def run_one(
    workdir: Path,
    card_title: str,
    station_checkout: str,
    project_key: str = None,
    project_paths: dict = None,
    expected_checkout: str = None,
) -> str:
    _ensure_clean_git_checkout(station_checkout)
    for checkout in (project_paths or {}).values():
        _ensure_clean_git_checkout(checkout)
    outbox_dir = workdir / "outbox"
    stub = workdir / "fake_orchestrator.py"
    stub.write_text(STUB_SOURCE, encoding="utf-8")

    os.environ["TRELLO_KEY"] = "verify-key"
    os.environ["TRELLO_TOKEN"] = "verify-token"
    os.environ["TRELLO_BOARD_ID"] = "verify-board"
    os.environ["AI_PM_PROVIDERS"] = "claude"
    # Root cause of the 2026-08-26 incident: this shell may still carry
    # AI_PM_SLACK_ENABLED=1 / a real SLACK_WEBHOOK_URL left over from a
    # previous production run (scripts/run-ai-project-manager.ps1 sets
    # both). notify() already defaults closed without AI_PM_SLACK_ENABLED,
    # but this "safe" script must not depend on that env staying unset -
    # clear both explicitly, the same way AI_ORCHESTRATOR_SPEC_DIR/
    # OUTBOX_DIR are pinned below against inherited production state.
    os.environ.pop("AI_PM_SLACK_ENABLED", None)
    os.environ.pop("SLACK_WEBHOOK_URL", None)
    # Same class of leak as above, for the controller finalizer added
    # later: this machine's live scheduler setup may already export a real
    # AI_ORCHESTRATOR_FINALIZE_CMD. Inheriting it would make main()'s real
    # build_finalize_fn() run the actual controller finalizer against this
    # script's throwaway (non-git) checkout directories, failing on
    # "cannot verify repository HEAD before execution" instead of proving
    # path resolution as intended.
    os.environ.pop("AI_ORCHESTRATOR_FINALIZE_CMD", None)
    os.environ["AI_ORCHESTRATOR_CMD"] = f'"{sys.executable}" "{stub}" "{outbox_dir}"'
    # Pinned explicitly (not left to the "specs"/"outbox" relative
    # defaults): this machine's own live scheduler setup already exports
    # AI_ORCHESTRATOR_SPEC_DIR/OUTBOX_DIR pointing at the real production
    # directories, which would otherwise silently win over cwd-relative
    # defaults and make this "safe" verification write/read real state.
    os.environ["AI_ORCHESTRATOR_SPEC_DIR"] = str(workdir / "specs")
    os.environ["AI_ORCHESTRATOR_OUTBOX_DIR"] = str(outbox_dir)
    os.environ["AI_PM_PROVIDER_STATE_PATH"] = str(workdir / "provider_state.json")
    # Deliberately does NOT contain any card's exact current title -
    # only the stable identity phrase (or, for the label-only case, the
    # project_key label - never anything derived from the title), as
    # recommended by resolve_project_path()'s docstring in
    # orchestrator_runner.py.
    os.environ["AI_PM_PROJECT_PATHS"] = json.dumps(
        project_paths if project_paths is not None else {"Station Agent": station_checkout}
    )

    project = ProjectRecord(
        name=card_title,
        priority=0,
        status=ProjectStatus.READY,
        orchestrator_ready_task="Revize MD/JSON",
        project_key=project_key or "Station Agent",
    )
    client = InMemoryTrelloClient()
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    cwd = os.getcwd()
    os.chdir(workdir)
    try:
        exit_code = main(["--once"], client=client)
    finally:
        os.chdir(cwd)

    if exit_code != 0:
        return f"FAIL card={card_title!r}: main() exited {exit_code}"

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)

    if reloaded.status != ProjectStatus.TESTING:
        return (
            f"FAIL card={card_title!r}: status={reloaded.status.value!r} "
            f"stop_reason={reloaded.stop_reason!r}"
        )
    checkout = expected_checkout or station_checkout
    expected = f"resolved project path: {checkout}"
    if reloaded.last_output != expected:
        return f"FAIL card={card_title!r}: last_output={reloaded.last_output!r}"
    return f"OK   card={card_title!r} -> {checkout}"


def main_entry() -> int:
    failures = 0
    with tempfile.TemporaryDirectory(prefix="verify-once-") as tmp:
        base = Path(tmp)
        station_checkout = str(base / "station-agent-checkout")
        pm_checkout = str(base / "ai-project-manager-checkout")
        total = len(CARD_TITLES) + 1
        for i, card_title in enumerate(CARD_TITLES):
            workdir = base / f"run-{i}"
            workdir.mkdir()
            result = run_one(workdir, card_title, station_checkout)
            print(result)
            if result.startswith("FAIL"):
                failures += 1

        # The actual real-board failure this fix targets: no phrase in the
        # title at all, resolved only via the project_key label.
        workdir = base / f"run-{len(CARD_TITLES)}"
        workdir.mkdir()
        result = run_one(
            workdir,
            LABEL_ONLY_CARD_TITLE,
            station_checkout,
            project_key="AI Project Manager",
            project_paths={"AI Project Manager": pm_checkout, "Station Agent": station_checkout},
            expected_checkout=pm_checkout,
        )
        print(result)
        if result.startswith("FAIL"):
            failures += 1
    if failures:
        print(f"\n{failures}/{total} card(s) failed to resolve.")
        return 1
    print(f"\nAll {total} card(s) resolved via identity match, no exact-title override needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())
