"""Live end-to-end proof that the persistent entrypoint's production
configuration wiring (the same env vars ``scripts/run-ai-project-manager.ps1``
loads from ``.secrets/scheduler.clixml`` and hands to
``ai_project_manager.watchdog`` - see ``start_ai_project_manager.bat`` and
``tests/test_scheduler_scripts.py``) actually produces a working tick, using
a real OS subprocess (not an in-process fake) that drives the real
``ai_project_manager.cli.main`` entrypoint end to end:

  - Slack: a real HTTP POST reaches a local server standing in for the
    webhook, with the expected start/done messages.
  - Trello: the project is read back through the real
    ``trello_sync.fetch_all_projects``/``project_from_card`` - the same code
    a subsequent tick would use - not merely trusted from in-memory state.
  - log: the real ``logging`` output the process produced, proving no
    credential value ever appears in it.
  - process: the real subprocess exit code.

Only the Trello client is a stand-in (``InMemoryTrelloClient``, injected the
same way ``cli.main`` allows tests to) so this suite never depends on a real
Trello board; everything else - config loading, scheduling, Slack HTTP call,
card read/write - is the genuine production code path.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import textwrap
import threading
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


class _SlackCapture(http.server.BaseHTTPRequestHandler):
    posts: list = []
    lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        with self.lock:
            self.posts.append(json.loads(body.decode("utf-8")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format, *args):  # noqa: A002 - silence test-server noise
        pass


def test_live_e2e_persistent_entrypoint_readback_slack_trello_log_process(tmp_path):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlackCapture)
    _SlackCapture.posts = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        webhook_url = f"http://{host}:{port}/webhook"

        harness = tmp_path / "harness.py"
        harness.write_text(_HARNESS_SOURCE, encoding="utf-8")
        readback_path = tmp_path / "trello_readback.json"

        secret_token = "super-secret-trello-token-should-never-be-logged"
        # Start from the real parent environment (not a hand-built minimal
        # one) so Windows-only subprocess/socket plumbing (SYSTEMROOT etc.)
        # still works; only override the specific PM/Trello/Slack settings
        # this test cares about.
        env = dict(os.environ)
        env.update(
            {
                "TRELLO_KEY": "dummy-trello-key",
                "TRELLO_TOKEN": secret_token,
                "TRELLO_BOARD_ID": "dummy-board",
                "SLACK_WEBHOOK_URL": webhook_url,
                "AI_PM_SLACK_ENABLED": "1",
                "AI_PM_PROVIDERS": "claude",
                # Keep this run fully self-contained instead of writing a
                # stray provider_state.json into the repo's own working
                # directory.
                "AI_PM_PROVIDER_STATE_PATH": str(tmp_path / "provider_state.json"),
            }
        )

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

        # -> Slack: real HTTP POSTs reached the local webhook stand-in - the
        # start notification and the implementation-to-testing notification.
        with _SlackCapture.lock:
            posts = list(_SlackCapture.posts)
        assert any("Zahajuji" in p.get("text", "") and "Persistent Entrypoint Demo" in p["text"] for p in posts)
        assert any("Průběžný stav" in p.get("text", "") and "Persistent Entrypoint Demo" in p["text"] for p in posts)
        assert not any(secret_token in p.get("text", "") for p in posts)

        # -> Trello: read back through the real fetch_all_projects/
        # project_from_card path, not trusted from in-memory state.
        readback = json.loads(readback_path.read_text(encoding="utf-8"))
        assert len(readback) == 1
        assert readback[0]["name"] == "Persistent Entrypoint Demo"
        assert readback[0]["status"] == "testing"
        assert readback[0]["last_output"] == "hotovo pres live e2e test"
    finally:
        server.shutdown()
        thread.join(timeout=5)
