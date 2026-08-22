import json
import subprocess

import pytest

from ai_project_manager.models import ProjectRecord, ProjectStatus
from ai_project_manager.orchestrator_handoff import InvalidTaskError
from ai_project_manager.orchestrator_runner import (
    DEFAULT_PROVIDER_AGENT_MAP,
    NO_COMMIT_INSTRUCTION,
    OrchestratorProcessError,
    ProjectPathError,
    build_run_fn,
    map_provider_to_agent,
    parse_spec_markdown,
    resolve_project_path,
    spec_file_path,
)
from ai_project_manager.providers import ProviderRegistry, ProviderState


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _args_to_dict(argv):
    result = {}
    it = iter(argv)
    for token in it:
        if token.startswith("--"):
            result[token[2:]] = next(it, None)
    return result


def write_outbox_result(outbox_dir, project_name, payload, run_id=None):
    """Write a fake outbox result, echoing back ``run_id`` the way the
    real ai-orchestrator process is expected to - the reader only ever
    accepts a result whose run_id matches the run it just launched."""
    outbox_dir.mkdir(parents=True, exist_ok=True)
    slug = project_name.strip().lower().replace(" ", "-")
    body = dict(payload)
    if run_id is not None:
        body["run_id"] = run_id
    path = outbox_dir / f"autonomous-{slug}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def make_run_fn(tmp_path, registry, subprocess_run=None, run_id_fn=None, **kwargs):
    spec_dir = tmp_path / "specs"
    outbox_dir = tmp_path / "outbox"

    def default_subprocess_run(command):
        return completed()

    return build_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={"Demo": str(tmp_path / "demo-checkout")},
        spec_dir=str(spec_dir),
        outbox_dir=str(outbox_dir),
        subprocess_run=subprocess_run or default_subprocess_run,
        run_id_fn=run_id_fn or (lambda: "fixed-run-id"),
        **kwargs,
    ), spec_dir, outbox_dir


# ---- resolving a project's local path (item 1) ------------------------

def test_resolve_project_path_uses_explicit_per_project_override():
    project = ProjectRecord(name="Demo")
    path = resolve_project_path(project, project_paths={"Demo": "/checkouts/demo"})
    assert path == "/checkouts/demo"


def test_resolve_project_path_falls_back_to_slugified_shared_root():
    project = ProjectRecord(name="My Cool Project")
    path = resolve_project_path(project, projects_root="/work")
    assert path.replace("\\", "/") == "/work/my-cool-project"


def test_resolve_project_path_raises_without_any_mapping_configured():
    project = ProjectRecord(name="Demo")
    with pytest.raises(ProjectPathError):
        resolve_project_path(project)


# ---- the real CLI argument shape (item 0 / regression test, item 6) ---

def test_run_fn_invokes_real_cli_with_project_goal_spec_and_agent(tmp_path):
    seen = {}
    registry = ProviderRegistry()
    registry.mark_available("claude")

    def fake_subprocess_run(command):
        seen["command"] = command
        # ai-orchestrator writes its result to the outbox, not stdout.
        outbox_dir = tmp_path / "outbox"
        write_outbox_result(
            outbox_dir, "Demo",
            {"checkpoint": {"step": 2}, "last_output": "did work", "next_step": "next", "status": "in_progress"},
            run_id="fixed-run-id",
        )
        return completed()

    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.READY,
        orchestrator_ready_task="Implement feature X",
        next_step="Wire up auth",
        checkpoint={"step": 1},
    )

    run_fn, spec_dir, outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)
    result = run_fn(project, "claude")

    command = seen["command"]
    assert command[0] == "ai-orchestrator"
    assert command[command.index("--project") + 1] == str(tmp_path / "demo-checkout")
    assert command[command.index("--goal") + 1] == "Implement feature X"
    # provider "claude" is Project Manager's own name; the ai-orchestrator
    # CLI expects its agent identifier, "claude-code" (item 5).
    assert command[command.index("--agent") + 1] == "claude-code"
    assert command[command.index("--run-id") + 1] == "fixed-run-id"

    spec_path = command[command.index("--spec") + 1]
    # build_run_fn resolves spec_dir to an absolute path internally, so
    # compare against that same resolved form.
    assert spec_path == str(spec_file_path(str(spec_dir.resolve()), "Demo"))
    assert spec_path.endswith(".md")
    spec_payload = parse_spec_markdown(open(spec_path, encoding="utf-8").read())
    assert spec_payload["goal"] == "Implement feature X"
    assert spec_payload["checkpoint"] == {"step": 1}
    assert spec_payload["provider"] == "claude-code"
    assert spec_payload["run_id"] == "fixed-run-id"
    assert "Wire up auth" in spec_payload["definition_of_done"]
    # No DoD line was ever left blank.
    assert all(item.strip() for item in spec_payload["definition_of_done"])

    assert result["checkpoint"] == {"step": 2}
    assert result["last_output"] == "did work"
    assert result["next_step"] == "next"
    assert result["status"] == "in_progress"


def test_spec_file_is_stable_across_runs_for_checkpoint_resume(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("claude")
    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X", checkpoint={"step": 1})

    def fake_subprocess_run(command):
        write_outbox_result(
            tmp_path / "outbox", "Demo", {"checkpoint": {"step": 2}, "status": "in_progress"},
            run_id="fixed-run-id",
        )
        return completed()

    run_fn, spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)

    run_fn(project, "claude")
    project.checkpoint = {"step": 2}
    run_fn(project, "claude")

    # Same path both times - ai-orchestrator resumes off a spec whose
    # identity never changes between runs for a given project.
    paths = list(spec_dir.glob("*.md"))
    assert len(paths) == 1
    assert parse_spec_markdown(paths[0].read_text(encoding="utf-8"))["checkpoint"] == {"step": 2}


def test_run_fn_marks_done_when_orchestrator_reports_done(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("claude")

    def fake_subprocess_run(command):
        write_outbox_result(
            tmp_path / "outbox", "Demo", {"done": True, "last_output": "shipped"}, run_id="fixed-run-id",
        )
        return completed()

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    run_fn, _spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)
    result = run_fn(project, "claude")

    assert result["status"] == "done"
    assert result["last_output"] == "shipped"


def test_run_fn_detects_limit_from_nonzero_exit_and_updates_registry(tmp_path):
    def fake_subprocess_run(command):
        return completed(stderr="Error: session limit exceeded, try later", returncode=1)

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X", checkpoint={"step": 4})
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn, _spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)
    result = run_fn(project, "claude")

    status = registry.get_status("claude")
    assert status.state == ProviderState.LIMITED
    assert status.retry_after is not None
    # Stays schedulable (not PAUSED) so it auto-resumes once the
    # provider is available again - the registry gates retries, not the
    # project status.
    assert result["status"] == "in_progress"
    assert "session limit" in result["stop_reason"]
    assert result["retry_after"] == status.retry_after.isoformat()


def test_run_fn_detects_limit_reported_explicitly_in_outbox_payload(tmp_path):
    def fake_subprocess_run(command):
        payload = {
            "limit_hit": "quota exceeded",
            "retry_after_seconds": 120,
            "checkpoint": {"step": 7},
        }
        write_outbox_result(tmp_path / "outbox", "Demo", payload, run_id="fixed-run-id")
        return completed()

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn, _spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)
    result = run_fn(project, "claude")

    status = registry.get_status("claude")
    assert status.state == ProviderState.LIMITED
    assert status.checkpoint == {"step": 7}
    assert result["checkpoint"] == {"step": 7}
    assert result["status"] == "in_progress"


def test_run_fn_raises_on_non_limit_failure_without_touching_registry(tmp_path):
    def fake_subprocess_run(command):
        return completed(stderr="unexpected crash: NullPointerException", returncode=1)

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn, _spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)

    with pytest.raises(OrchestratorProcessError):
        run_fn(project, "claude")

    assert registry.get_status("claude").state == ProviderState.AVAILABLE


def test_run_fn_raises_when_no_outbox_result_was_produced(tmp_path):
    def fake_subprocess_run(command):
        # process exits cleanly but never wrote an outbox result
        return completed()

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn, _spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)

    with pytest.raises(OrchestratorProcessError):
        run_fn(project, "claude")


def test_run_fn_raises_on_non_json_outbox_result(tmp_path):
    def fake_subprocess_run(command):
        outbox_dir = tmp_path / "outbox"
        outbox_dir.mkdir(parents=True, exist_ok=True)
        (outbox_dir / "autonomous-demo.json").write_text("not json at all", encoding="utf-8")
        return completed()

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn, _spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)

    with pytest.raises(OrchestratorProcessError):
        run_fn(project, "claude")


def test_run_fn_detects_limit_from_raised_subprocess_exception(tmp_path):
    def fake_subprocess_run(command):
        raise RuntimeError("429 too many requests")

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn, _spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)
    result = run_fn(project, "claude")

    assert registry.get_status("claude").state == ProviderState.LIMITED
    assert result["status"] == "in_progress"


def test_run_fn_raises_when_project_path_cannot_be_resolved(tmp_path):
    project = ProjectRecord(name="Unmapped", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn = build_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={},
        projects_root=None,
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(tmp_path / "outbox"),
        subprocess_run=lambda command: completed(),
    )

    with pytest.raises(OrchestratorProcessError):
        run_fn(project, "claude")


# ---- refusing an empty task/DoD without spending any AI tokens (item 1)

def test_run_fn_never_spawns_the_subprocess_for_an_empty_project(tmp_path):
    subprocess_calls = []

    def fake_subprocess_run(command):
        subprocess_calls.append(command)
        return completed()

    project = ProjectRecord(name="Demo")  # no task, no main_task, no DoD material at all
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn, _spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)

    with pytest.raises(InvalidTaskError):
        run_fn(project, "claude")

    assert subprocess_calls == []


# ---- pairing the outbox result to this run's run_id, not the project
# name/pattern (item 4) ------------------------------------------------

def test_run_fn_ignores_stale_result_with_a_different_run_id_and_raises(tmp_path):
    def fake_subprocess_run(command):
        # A leftover/foreign result sitting in the outbox for the same
        # project slug, from a run this call never launched.
        write_outbox_result(
            tmp_path / "outbox", "Demo", {"status": "done", "last_output": "stale"}, run_id="some-other-run-id",
        )
        return completed()

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn, _spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)

    with pytest.raises(OrchestratorProcessError):
        run_fn(project, "claude")


def test_run_fn_picks_the_matching_run_id_even_when_a_stale_result_is_newer(tmp_path):
    def fake_subprocess_run(command):
        outbox_dir = tmp_path / "outbox"
        outbox_dir.mkdir(parents=True, exist_ok=True)
        (outbox_dir / "autonomous-demo-a.json").write_text(
            json.dumps({"status": "in_progress", "last_output": "old", "run_id": "fixed-run-id"}),
            encoding="utf-8",
        )
        # Written after (and would win any "newest mtime" heuristic), but
        # belongs to a different run and must never be picked over it.
        (outbox_dir / "autonomous-demo-b.json").write_text(
            json.dumps({"status": "done", "last_output": "foreign", "run_id": "other-run"}),
            encoding="utf-8",
        )
        return completed()

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn, _spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)
    result = run_fn(project, "claude")

    assert result["last_output"] == "old"


def test_run_fn_generates_a_fresh_run_id_per_call(tmp_path):
    seen_run_ids = []
    registry = ProviderRegistry()
    registry.mark_available("claude")

    def fake_subprocess_run(command):
        run_id = _args_to_dict(command)["run-id"]
        seen_run_ids.append(run_id)
        write_outbox_result(tmp_path / "outbox", "Demo", {"status": "in_progress"}, run_id=run_id)
        return completed()

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    # Deliberately not overriding run_id_fn here - exercises the real
    # uuid-based default so each call gets a distinct run_id.
    run_fn = build_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={"Demo": str(tmp_path / "demo-checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(tmp_path / "outbox"),
        subprocess_run=fake_subprocess_run,
    )

    run_fn(project, "claude")
    run_fn(project, "claude")

    assert len(seen_run_ids) == 2
    assert seen_run_ids[0] != seen_run_ids[1]


# ---- the agent must never create its own git commit (item 10) --------

def test_spec_file_instructs_the_agent_never_to_commit(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("claude")

    def fake_subprocess_run(command):
        write_outbox_result(
            tmp_path / "outbox", "Demo", {"status": "in_progress"}, run_id="fixed-run-id",
        )
        return completed()

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    run_fn, spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)
    run_fn(project, "claude")

    spec_path = spec_file_path(str(spec_dir.resolve()), "Demo")
    spec_text = spec_path.read_text(encoding="utf-8")
    assert NO_COMMIT_INSTRUCTION in spec_text

    spec_payload = parse_spec_markdown(spec_text)
    assert "git commit" in spec_payload["constraints"]


# ---- provider -> agent identifier mapping (item 5) ---------------------

def test_map_provider_to_agent_translates_claude_to_claude_code():
    assert map_provider_to_agent("claude") == "claude-code"
    assert DEFAULT_PROVIDER_AGENT_MAP["claude"] == "claude-code"


def test_map_provider_to_agent_passes_through_unknown_providers():
    assert map_provider_to_agent("gpt") == "gpt"
    assert map_provider_to_agent("gemini") == "gemini"


def test_run_fn_keeps_pm_side_provider_name_for_registry_while_mapping_agent_for_cli(tmp_path):
    """The provider-registry name run_fn is called with (and everything
    it drives - locking, retry_after) stays "claude"; only the CLI/spec
    agent identifier is translated."""
    seen = {}
    registry = ProviderRegistry()
    registry.mark_available("claude")

    def fake_subprocess_run(command):
        seen["command"] = command
        return completed(stderr="429 too many requests", returncode=1)

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    run_fn, _spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)
    result = run_fn(project, "claude")

    assert seen["command"][seen["command"].index("--agent") + 1] == "claude-code"
    # The registry/limit bookkeeping is still keyed by "claude", not
    # "claude-code" - the Project Manager's own stable provider name.
    assert registry.get_status("claude").state == ProviderState.LIMITED
    assert registry.get_status("claude-code").state == ProviderState.AVAILABLE
    assert result["status"] == "in_progress"
