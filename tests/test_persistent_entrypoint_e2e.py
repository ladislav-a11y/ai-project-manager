"""Live end-to-end proof that the persistent entrypoint's production
configuration wiring (the same env vars ``scripts/run-ai-project-manager.ps1``
loads from ``.secrets/scheduler.clixml`` and hands to
``ai_project_manager.watchdog`` - see ``start_ai_project_manager.bat`` and
``tests/test_scheduler_scripts.py``) actually produces a working tick, using
a real OS subprocess (not an in-process fake) that drives the real
``ai_project_manager.cli.main`` entrypoint end to end:

  - Trello: the project is read back through the real
    ``trello_sync.fetch_all_projects``/``project_from_card`` - the same code
    a subsequent tick would use - not merely trusted from in-memory state.
  - log: the real ``logging`` output the process produced, proving no
    credential value ever appears in it.
  - process: the real subprocess exit code.

Only the Trello client is a stand-in (``InMemoryTrelloClient``, injected the
same way ``cli.main`` allows tests to) so this suite never depends on a real
Trello board; everything else - config loading, scheduling, card read/write -
is the genuine production code path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_HARNESS_SOURCE = textwrap.dedent(
    """
    import json
    import sys
    sys.path.insert(0, {repo_root!r})

    from ai_project_manager import cli
    from ai_project_manager.models import ProjectRecord, ProjectStatus
    from ai_project_manager.trello_client import InMemoryTrelloClient
    from ai_project_manager.trello_sync import fetch_all_projects, sync_project_to_trello

    readback_path = sys.argv[1]

    client = InMemoryTrelloClient()
    project = ProjectRecord(
        name="Persistent Entrypoint Demo",
        priority=3,
        status=ProjectStatus.READY,
        main_task="prove the persistent entrypoint works end to end",
    )
    sync_project_to_trello(client, project)

    def run_fn(project, provider):
        return {{
            "status": "done",
            "checkpoint": {{"completed_dod_indices": [0]}},
            "last_output": "hotovo pres live e2e test",
            "next_step": "",
            "active_provider": provider,
        }}

    exit_code = cli.main(["--once", "--log-level", "INFO"], client=client, run_fn=run_fn)

    readback = [
        {{"name": p.name, "status": p.status.value, "last_output": p.last_output}}
        for p in fetch_all_projects(client)
    ]
    with open(readback_path, "w", encoding="utf-8") as fh:
        json.dump(readback, fh)

    sys.exit(exit_code)
    """
).format(repo_root=str(REPO_ROOT))


def test_live_e2e_persistent_entrypoint_readback_trello_log_process(tmp_path):
    harness = tmp_path / "harness.py"
    harness.write_text(_HARNESS_SOURCE, encoding="utf-8")
    readback_path = tmp_path / "trello_readback.json"

    secret_token = "super-secret-trello-token-should-never-be-logged"
    # Start from the real parent environment (not a hand-built minimal
    # one) so Windows-only subprocess/socket plumbing (SYSTEMROOT etc.)
    # still works; only override the specific PM/Trello settings this test
    # cares about.
    env = dict(os.environ)
    env.update(
        {
            "TRELLO_KEY": "dummy-trello-key",
            "TRELLO_TOKEN": secret_token,
            "TRELLO_BOARD_ID": "dummy-board",
            "AI_PM_PROVIDERS": "claude",
            # Keep this run fully self-contained instead of writing a
            # stray provider_state.json into the repo's own working
            # directory.
            "AI_PM_PROVIDER_STATE_PATH": str(tmp_path / "provider_state.json"),
        }
    )
    # Do not inherit the production launcher's multi-provider model catalog
    # into this single-provider test process.
    env.pop("AI_PM_PROVIDER_MODELS", None)
    # Nor its real controller finalize command: this machine's live scheduler
    # setup may already export AI_ORCHESTRATOR_FINALIZE_CMD. Inheriting it
    # would make main()'s real build_finalize_fn() run the actual controller
    # finalizer for a project with no configured checkout at all, failing
    # closed instead of reaching the "testing" status this test verifies.
    env.pop("AI_ORCHESTRATOR_FINALIZE_CMD", None)

    result = subprocess.run(
        [sys.executable, str(harness), str(readback_path)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(tmp_path),
    )

    # -> process: the real subprocess completed a real --once tick.
    assert result.returncode == 0, result.stdout + result.stderr

    # -> log: real logging.basicConfig output from cli.main, proving the
    # entrypoint actually ran a tick - and never contains the credential.
    combined_log = result.stdout + result.stderr
    assert "starting ai-project-manager" in combined_log
    assert "--once tick complete" in combined_log
    assert secret_token not in combined_log

    # -> Trello: read back through the real fetch_all_projects/
    # project_from_card path, not trusted from in-memory state.
    readback = json.loads(readback_path.read_text(encoding="utf-8"))
    assert len(readback) == 1
    assert readback[0]["name"] == "Persistent Entrypoint Demo"
    assert readback[0]["status"] == "testing"
    assert readback[0]["last_output"] == "hotovo pres live e2e test"
