import json
import re
import subprocess

import pytest

from ai_project_manager.models import DoDItem, ProjectRecord, ProjectStatus
from ai_project_manager.orchestrator_handoff import InvalidTaskError
from ai_project_manager.orchestrator_runner import (
    DEFAULT_PROVIDER_AGENT_MAP,
    NO_COMMIT_INSTRUCTION,
    OrchestratorProcessError,
    ProjectPathError,
    build_audit_run_fn,
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


def test_audit_run_fn_uses_supported_autonomous_cli_and_reads_internal_audit(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("claude")
    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.TESTING,
        orchestrator_ready_task="Verify the feature",
        dod=[DoDItem(text="implemented", checked=True)],
        checkpoint={"completed_dod_indices": [0]},
    )
    seen = {}

    def fake_subprocess_run(command):
        seen["command"] = command
        write_outbox_result(
            tmp_path / "outbox",
            "Demo",
            {
                "status": "completed",
                "last_output": "tests and independent audit passed",
                "iterations": [{
                    "audit_performed": True,
                    "audit_rejected_indices": [],
                    "audit_protocol_error": False,
                    "test_output": "1 passed",
                }],
            },
            run_id="audit-run",
        )
        return completed()

    result = build_audit_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={"Demo": str(tmp_path / "demo-checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(tmp_path / "outbox"),
        subprocess_run=fake_subprocess_run,
        run_id_fn=lambda: "audit-run",
    )(project, "claude")

    assert result["verdict"] == "accepted"
    assert "--mode" not in seen["command"]
    assert seen["command"][seen["command"].index("--max-iterations") + 1] == "1"
    assert seen["command"][-1] == "--no-commit"


def test_audit_run_fn_reads_internal_audit_rejection_with_concrete_reason(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("claude")
    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.TESTING,
        orchestrator_ready_task="Verify the feature",
        dod=[DoDItem(text="implemented", checked=True)],
        checkpoint={"completed_dod_indices": [0]},
    )

    def fake_subprocess_run(command):
        write_outbox_result(
            tmp_path / "outbox",
            "Demo",
            {
                "status": "completed",
                "iterations": [{
                    "audit_performed": True,
                    "audit_rejected_indices": [0],
                    "audit_protocol_error": False,
                    "note": "export still times out on large accounts",
                    "test_output": "1 failed",
                }],
            },
            run_id="audit-run",
        )
        return completed()

    result = build_audit_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={"Demo": str(tmp_path / "demo-checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(tmp_path / "outbox"),
        subprocess_run=fake_subprocess_run,
        run_id_fn=lambda: "audit-run",
    )(project, "claude")

    assert result["verdict"] == "rejected"
    assert "0" in result["reason"]
    assert "export still times out on large accounts" in result["reason"]
    assert result["reject_target"] == "in_progress"


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


@pytest.mark.parametrize(
    "card_title",
    [
        "P5 - Station Agent (čeká)",
        "P4 — Station Agent checkpoint (čeká po PM/Orchestrator auditu)",
        "P4 — Station Agent: DX Cluster OK stav, ale kandidáti se ztrácejí",
        "P0: Station Agent - revize MD/JSON",
        "P2 Station Agent",
    ],
)
def test_resolve_project_path_matches_identity_across_priority_and_wording_changes(card_title):
    """A single ``AI_PM_PROJECT_PATHS`` entry keyed by the project's stable
    identity ("Station Agent") must resolve every card whose title carries
    a changing P0-P5 prefix and/or changing descriptive suffix - without
    ever requiring the exact current card title to be added."""
    project = ProjectRecord(name=card_title)
    path = resolve_project_path(project, project_paths={"Station Agent": "/checkouts/station-agent"})
    assert path == "/checkouts/station-agent"


def test_resolve_project_path_still_honors_exact_title_override():
    project = ProjectRecord(name="P5 - Station Agent (čeká)")
    path = resolve_project_path(
        project,
        project_paths={
            "Station Agent": "/checkouts/station-agent",
            "P5 - Station Agent (čeká)": "/checkouts/station-agent-special",
        },
    )
    assert path == "/checkouts/station-agent-special"


def test_resolve_project_path_prefers_the_longer_more_specific_identity_match():
    project = ProjectRecord(name="P4 — Station Agent checkpoint (čeká po PM/Orchestrator auditu)")
    path = resolve_project_path(
        project,
        project_paths={
            "Station Agent": "/checkouts/station-agent",
            "Station Agent checkpoint": "/checkouts/station-agent-checkpoint",
        },
    )
    assert path == "/checkouts/station-agent-checkpoint"


def test_resolve_project_path_does_not_match_a_substring_inside_another_word():
    project = ProjectRecord(name="P1 - Agentura pro digitalizaci")
    with pytest.raises(ProjectPathError):
        resolve_project_path(project, project_paths={"Agent": "/checkouts/station-agent"})


def test_resolve_project_path_raises_on_ambiguous_equally_specific_matches():
    project = ProjectRecord(name="P3 - Foo Bar")
    with pytest.raises(ProjectPathError):
        resolve_project_path(
            project,
            project_paths={
                "Foo": "/checkouts/foo-repo",
                "Bar": "/checkouts/bar-repo",
            },
        )


def test_resolve_project_path_strips_priority_prefix_before_slugifying_shared_root():
    project = ProjectRecord(name="P2 - My Cool Project")
    path = resolve_project_path(project, projects_root="/work")
    assert path.replace("\\", "/") == "/work/my-cool-project"


_STABLE_IDENTITY_PROJECT_PATHS = {
    "AI Project Manager": "/checkouts/ai-project-manager",
    "AI Orchestrator": "/checkouts/ai-orchestrator",
    "Station Agent": "/checkouts/station-agent",
}


@pytest.mark.parametrize("priority", [0, 1, 2, 3, 4, 5])
@pytest.mark.parametrize(
    "project_key,expected_path",
    [
        ("AI Project Manager", "/checkouts/ai-project-manager"),
        ("AI Orchestrator", "/checkouts/ai-orchestrator"),
        ("Station Agent", "/checkouts/station-agent"),
    ],
)
def test_resolve_project_path_uses_stable_label_identity_regardless_of_title(
    priority, project_key, expected_path
):
    """Root-cause regression: a work card's title is free-form status
    prose that need not ever mention the project it belongs to (e.g. the
    real card "P5 - Izolace testovacich Slack notifikaci" for AI Project
    Manager). Phrase-in-title matching against ``project.name`` can never
    handle that; ``project.project_key`` - a plain Trello label,
    independent of both title and P0-P5 priority - must."""
    project = ProjectRecord(
        name=f"P{priority} - Izolace testovacich Slack notifikaci",
        priority=priority,
        project_key=project_key,
    )
    path = resolve_project_path(project, project_paths=_STABLE_IDENTITY_PROJECT_PATHS)
    assert path == expected_path


def test_resolve_project_path_label_identity_wins_over_a_misleading_title_phrase():
    """Safety requirement: a work card must never accidentally resolve to
    the wrong repository. Here the title happens to mention a different
    project's identity phrase than the card's actual (labeled) project -
    the explicit label must win, not the incidental title text."""
    project = ProjectRecord(
        name="P2 - Station Agent needs a fix from AI Orchestrator",
        priority=2,
        project_key="Station Agent",
    )
    path = resolve_project_path(project, project_paths=_STABLE_IDENTITY_PROJECT_PATHS)
    assert path == "/checkouts/station-agent"


def test_resolve_project_path_without_project_key_falls_back_to_title_phrase_matching():
    """Cards created before the project_key label existed keep working via
    the legacy title-phrase fallback."""
    project = ProjectRecord(name="P5 - Station Agent (čeká)", priority=5)
    path = resolve_project_path(project, project_paths=_STABLE_IDENTITY_PROJECT_PATHS)
    assert path == "/checkouts/station-agent"


def test_resolve_project_path_exact_title_override_still_wins_over_project_key():
    project = ProjectRecord(
        name="P5 - Izolace testovacich Slack notifikaci",
        priority=5,
        project_key="AI Project Manager",
    )
    path = resolve_project_path(
        project,
        project_paths={
            **_STABLE_IDENTITY_PROJECT_PATHS,
            "P5 - Izolace testovacich Slack notifikaci": "/checkouts/special-override",
        },
    )
    assert path == "/checkouts/special-override"


def test_resolve_project_path_distinguishes_similarly_prefixed_projects():
    project_paths = {
        "Řídicí systém": "/checkouts/ai-project-manager",
        "AI Project Manager": "/checkouts/ai-project-manager",
        "ai-orchestrator": "/checkouts/ai-orchestrator",
    }
    pm_project = ProjectRecord(name="P1 - Audit a stabilizace AI Project Manager")
    orch_project = ProjectRecord(name="P1 - Audit a stabilizace ai-orchestrator")
    assert resolve_project_path(pm_project, project_paths=project_paths) == "/checkouts/ai-project-manager"
    assert resolve_project_path(orch_project, project_paths=project_paths) == "/checkouts/ai-orchestrator"


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


def test_parse_spec_markdown_does_not_double_count_dod_items_embedded_in_goal_text(tmp_path):
    """A project's Goal text is very often the raw Trello card description
    verbatim (see orchestrator_handoff._goal_text), which can itself
    contain "- [ ] ..." checklist markdown identical to the real
    Definition of Done. The DoD must only ever be read from the "##
    Definition of Done" section - never scanned across the whole spec
    file - or every item is silently counted twice (regression: Trello
    8 bodu -> spec 16 instead of 8)."""
    registry = ProviderRegistry()
    registry.mark_available("claude")

    def fake_subprocess_run(command):
        write_outbox_result(
            tmp_path / "outbox", "Demo", {"status": "in_progress"}, run_id="fixed-run-id",
        )
        return completed()

    project = ProjectRecord(
        name="Demo",
        orchestrator_ready_task=(
            "CIL: overit checklist.\n\nDEFINITION OF DONE:\n"
            + "\n".join(f"- [ ] bod {i}" for i in range(1, 9))
        ),
    )
    run_fn, spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)
    run_fn(project, "claude")

    spec_path = spec_file_path(str(spec_dir.resolve()), "Demo")
    spec_payload = parse_spec_markdown(spec_path.read_text(encoding="utf-8"))

    assert spec_payload["definition_of_done"] == [f"bod {i}" for i in range(1, 9)]
    # The real ai-orchestrator currently scans checkbox lines across the whole
    # document.  Goal therefore must not contain a second checkbox copy.
    rendered = spec_path.read_text(encoding="utf-8")
    assert len(re.findall(r"(?m)^[-*]\s*\[[ xX]\]\s+", rendered)) == 8
    assert "- bod 1" in parse_spec_markdown(rendered)["goal"]


def test_spec_marks_checkpoint_verified_dod_as_checked(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("claude")

    def fake_subprocess_run(command):
        write_outbox_result(tmp_path / "outbox", "Demo", {"status": "in_progress"}, run_id="fixed-run-id")
        return completed()

    project = ProjectRecord(
        name="Demo",
        orchestrator_ready_task="Goal text",
        checkpoint={"completed_dod_indices": [0]},
        dod=[DoDItem(text="first"), DoDItem(text="second")],
    )
    run_fn, spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)
    run_fn(project, "claude")

    rendered = spec_file_path(str(spec_dir.resolve()), "Demo").read_text(encoding="utf-8")
    assert "- [x] first" in rendered
    assert "- [ ] second" in rendered


def test_run_fn_uses_project_key_for_stable_spec_path_across_card_title_edits(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("claude")
    seen_specs = []

    def fake_subprocess_run(command):
        seen_specs.append(command[command.index("--spec") + 1])
        write_outbox_result(
            tmp_path / "outbox",
            "ignored-by-run-id",
            {"status": "completed"},
            run_id="fixed-run-id",
        )
        return completed()

    run_fn, spec_dir, _outbox_dir = make_run_fn(
        tmp_path, registry, subprocess_run=fake_subprocess_run
    )
    first = ProjectRecord(
        name="P2 - Implementace prihlaseni",
        project_key="Demo",
        status=ProjectStatus.READY,
        orchestrator_ready_task="Implement feature X",
    )
    renamed = ProjectRecord(
        name="P5 - Implementace prihlaseni (kontrola)",
        project_key="Demo",
        status=ProjectStatus.READY,
        orchestrator_ready_task="Implement feature X",
    )

    run_fn(first, "claude")
    run_fn(renamed, "claude")

    expected = str(spec_file_path(str(spec_dir.resolve()), "unused", "Demo"))
    assert seen_specs == [expected, expected]

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


def test_spec_file_round_trips_a_checkpoint_containing_html_comment_close(tmp_path):
    """Regression: the checkpoint can carry arbitrary agent-produced data
    (a diff, partial output, ...) and can very plausibly contain the
    literal substring "-->", which would otherwise prematurely close the
    fenced PM-CHECKPOINT comment, truncate the embedded JSON, and corrupt
    the spec file handed to the real external ai-orchestrator process on
    every resume - the same failure mode fixed for trello_sync.py's
    PM-DATA block."""
    registry = ProviderRegistry()
    registry.mark_available("claude")
    checkpoint = {"diff": "before --> after", "note": "see <!-- comment --> here"}
    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X", checkpoint=checkpoint)

    def fake_subprocess_run(command):
        write_outbox_result(
            tmp_path / "outbox", "Demo", {"status": "in_progress"}, run_id="fixed-run-id",
        )
        return completed()

    run_fn, spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)
    run_fn(project, "claude")

    spec_path = spec_file_path(str(spec_dir.resolve()), "Demo")
    spec_text = spec_path.read_text(encoding="utf-8")
    # The raw file that ai-orchestrator actually reads must never contain
    # a stray "-->" inside the comment body - only the real closing one.
    assert spec_text.count("-->") == 1

    spec_payload = parse_spec_markdown(spec_text)
    assert spec_payload["checkpoint"] == checkpoint


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
    # A provider limit is a recoverable wait and must be visible in
    # Trello's Čeká na AI phase, with the checkpoint retained for resume.
    assert result["status"] == "paused"
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
    assert result["status"] == "paused"


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


def test_run_fn_raises_controlled_error_for_non_object_outbox_result(tmp_path):
    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")
    run_fn, _spec_dir, _outbox_dir = make_run_fn(
        tmp_path,
        registry,
        read_outbox=lambda _directory, _project, _run_id: [],
    )

    with pytest.raises(
        OrchestratorProcessError,
        match="outbox result must be an object, got list",
    ):
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
    assert result["status"] == "paused"


def test_run_fn_treats_a_subprocess_timeout_as_a_failure_not_a_hang(tmp_path):
    """A hung ai-orchestrator/agent process must never wedge the
    scheduler loop forever - subprocess.TimeoutExpired is just another
    exception the generic ``except Exception`` in run_fn already turns
    into an OrchestratorProcessError (or a provider-limit result, if it
    looks like one)."""
    def fake_subprocess_run(command):
        raise subprocess.TimeoutExpired(cmd=command, timeout=30)

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn, _spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)

    with pytest.raises(OrchestratorProcessError):
        run_fn(project, "claude")

    assert registry.get_status("claude").state == ProviderState.AVAILABLE


def test_run_fn_does_not_mistake_429_inside_a_timeout_command_path_for_a_rate_limit(tmp_path):
    """A temp directory/UUID can contain the digits 429 by chance.  Only a
    standalone HTTP status is a limit signal; digits embedded in a path must
    not convert an unrelated subprocess timeout into a provider-limit result.
    """
    def fake_subprocess_run(command):
        raise subprocess.TimeoutExpired(cmd=[r"C:\\temp\\uuid-a429b\\agent.exe"], timeout=30)

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")
    run_fn, _spec_dir, _outbox_dir = make_run_fn(
        tmp_path, registry, subprocess_run=fake_subprocess_run
    )

    with pytest.raises(OrchestratorProcessError):
        run_fn(project, "claude")

    assert registry.get_status("claude").state == ProviderState.AVAILABLE


def test_build_run_fn_applies_configured_timeout_to_the_real_subprocess_call(tmp_path, monkeypatch):
    """AI_ORCHESTRATOR_TIMEOUT_SECONDS (build_run_fn's timeout_seconds)
    must actually reach subprocess.run - previously it was parsed into
    Config but never wired anywhere, so a hung real ai-orchestrator
    process could block the scheduler forever."""
    seen = {}

    def fake_run(command, capture_output, text, check, timeout):
        seen["timeout"] = timeout
        return completed()

    monkeypatch.setattr("ai_project_manager.orchestrator_runner.subprocess.run", fake_run)

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn = build_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={"Demo": str(tmp_path / "demo-checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(tmp_path / "outbox"),
        timeout_seconds=45.0,
        run_id_fn=lambda: "fixed-run-id",
        read_outbox=lambda outbox_dir, project_name, run_id: {"status": "in_progress"},
    )
    run_fn(project, "claude")

    assert seen["timeout"] == 45.0


def test_build_run_fn_without_timeout_seconds_passes_no_timeout(tmp_path, monkeypatch):
    seen = {}

    def fake_run(command, capture_output, text, check, timeout):
        seen["timeout"] = timeout
        return completed()

    monkeypatch.setattr("ai_project_manager.orchestrator_runner.subprocess.run", fake_run)

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")

    run_fn = build_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={"Demo": str(tmp_path / "demo-checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(tmp_path / "outbox"),
        run_id_fn=lambda: "fixed-run-id",
        read_outbox=lambda outbox_dir, project_name, run_id: {"status": "in_progress"},
    )
    run_fn(project, "claude")

    assert seen["timeout"] is None


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


def test_run_fn_skips_newer_non_object_outbox_candidate(tmp_path):
    def fake_subprocess_run(command):
        outbox_dir = tmp_path / "outbox"
        outbox_dir.mkdir(parents=True, exist_ok=True)
        valid = outbox_dir / "autonomous-demo-valid.json"
        valid.write_text(
            json.dumps({
                "status": "in_progress",
                "last_output": "matching object",
                "run_id": "fixed-run-id",
            }),
            encoding="utf-8",
        )
        # The real-contract filename is always checked first, regardless of
        # mtimes, so this proves the malformed candidate is skipped before
        # the compatibility scan finds the usable result below.
        invalid = outbox_dir / "autonomous-fixed-run-id.json"
        invalid.write_text(
            json.dumps([{"run_id": "fixed-run-id"}]),
            encoding="utf-8",
        )
        return completed()

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("claude")
    run_fn, _spec_dir, _outbox_dir = make_run_fn(
        tmp_path, registry, subprocess_run=fake_subprocess_run
    )

    result = run_fn(project, "claude")

    assert result["last_output"] == "matching object"


def test_run_fn_reads_real_run_id_named_outbox_file(tmp_path):
    def fake_subprocess_run(command):
        outbox_dir = tmp_path / "outbox"
        outbox_dir.mkdir(parents=True, exist_ok=True)
        (outbox_dir / "autonomous-fixed-run-id.json").write_text(
            json.dumps({"status": "completed", "run_id": "fixed-run-id"}),
            encoding="utf-8",
        )
        return completed()

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("auto")
    run_fn, _spec_dir, _outbox_dir = make_run_fn(
        tmp_path, registry, subprocess_run=fake_subprocess_run
    )

    result = run_fn(project, "auto")

    assert result["status"] == "done"


def test_nonzero_cli_exit_with_valid_max_iterations_outbox_is_progress_not_crash(tmp_path):
    def fake_subprocess_run(command):
        outbox_dir = tmp_path / "outbox"
        outbox_dir.mkdir(parents=True, exist_ok=True)
        (outbox_dir / "autonomous-fixed-run-id.json").write_text(
            json.dumps({
                "status": "max_iterations",
                "run_id": "fixed-run-id",
                "checkpoint": {"completed_dod_indices": [0]},
                "next_step": "finish item 1",
            }),
            encoding="utf-8",
        )
        return completed(stderr="max iterations", returncode=1)

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("auto")
    run_fn, _spec_dir, _outbox_dir = make_run_fn(
        tmp_path, registry, subprocess_run=fake_subprocess_run
    )

    result = run_fn(project, "auto")

    assert result["status"] == "in_progress"
    assert result["checkpoint"] == {"completed_dod_indices": [0]}
    assert result["next_step"] == "finish item 1"


def test_protocol_error_outbox_maps_to_blocked(tmp_path):
    def fake_subprocess_run(command):
        outbox_dir = tmp_path / "outbox"
        outbox_dir.mkdir(parents=True, exist_ok=True)
        (outbox_dir / "autonomous-fixed-run-id.json").write_text(
            json.dumps({
                "status": "protocol_error",
                "run_id": "fixed-run-id",
                "error": "agent repeatedly returned invalid DoD JSON",
                "checkpoint": {"completed_dod_indices": [0]},
            }),
            encoding="utf-8",
        )
        return completed(stderr="protocol error", returncode=1)

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    registry = ProviderRegistry()
    registry.mark_available("auto")
    run_fn, _spec_dir, _outbox_dir = make_run_fn(
        tmp_path, registry, subprocess_run=fake_subprocess_run
    )

    result = run_fn(project, "auto")

    assert result["status"] == "blocked"
    assert result["checkpoint"] == {"completed_dod_indices": [0]}
    assert "invalid DoD JSON" in result["stop_reason"]

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


def test_spec_goal_does_not_duplicate_compact_embedded_dod(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("claude")

    def fake_subprocess_run(command):
        write_outbox_result(tmp_path / "outbox", "Demo", {"status": "in_progress"}, run_id="fixed-run-id")
        return completed()

    project = ProjectRecord(
        name="Demo",
        orchestrator_ready_task="Goal text\nDEFINITION OF DONE: [ ] first [ ] second",
        dod=[DoDItem(text="first"), DoDItem(text="second")],
    )
    run_fn, spec_dir, _outbox_dir = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)
    run_fn(project, "claude")

    spec_text = spec_file_path(str(spec_dir.resolve()), "Demo").read_text(encoding="utf-8")
    assert spec_text.count("[ ] first") == 1
    assert spec_text.count("[ ] second") == 1


def test_spec_encodes_fixed_authority_chain_and_orchestrator_only_audit(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("claude")

    def fake_subprocess_run(command):
        write_outbox_result(
            tmp_path / "outbox", "Demo", {"status": "in_progress"}, run_id="fixed-run-id",
        )
        return completed()

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    run_fn, spec_dir, _ = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)
    run_fn(project, "claude")

    payload = parse_spec_markdown(
        spec_file_path(str(spec_dir.resolve()), "Demo").read_text("utf-8")
    )
    assert payload["governance"] == {
        "source_of_truth": "trello",
        "control_hierarchy": ["ai-project-manager", "ai-orchestrator", "agents"],
        "audit_authority": "ai-orchestrator",
    }
    assert "Only ai-orchestrator may perform the audit" in payload["constraints"]


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
    assert result["status"] == "paused"
