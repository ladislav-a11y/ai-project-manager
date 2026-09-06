import json
import re
import subprocess
from datetime import timedelta

import pytest

from ai_project_manager.models import DoDItem, ProjectRecord as _ProjectRecord, ProjectStatus
from ai_project_manager.orchestrator_handoff import InvalidTaskError
from ai_project_manager.orchestrator_runner import (
    DEFAULT_PROVIDER_AGENT_MAP,
    INBOX_PLANNER_PROVIDERS,
    NO_COMMIT_INSTRUCTION,
    OrchestratorProcessError,
    ProjectPathError,
    build_audit_run_fn,
    build_finalize_fn,
    build_inbox_planner_fn,
    build_run_fn,
    _tick_provider_order,
    _controller_finalization_is_verified,
    _finalization_needs_refresh,
    _terminal_finalization_issue,
    map_provider_to_agent,
    parse_spec_markdown,
    resolve_project_path,
    spec_file_path,
)
from ai_project_manager.providers import ProviderRegistry, ProviderState


def ProjectRecord(*args, **kwargs):
    """Build test records with the explicit identity used by Demo fixtures."""
    if kwargs.get("name") == "Demo" and "project_key" not in kwargs:
        kwargs["project_key"] = "Demo"
    return _ProjectRecord(*args, **kwargs)


def test_inbox_planner_provider_allowlist_never_contains_hermes():
    assert INBOX_PLANNER_PROVIDERS == ("antigravity", "claude-code", "codex")
    assert "hermes" not in INBOX_PLANNER_PROVIDERS


@pytest.fixture(autouse=True)
def _existing_demo_checkout(tmp_path):
    """The production dispatcher now requires its allowlisted path to exist."""
    (tmp_path / "demo-checkout").mkdir()


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
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", project_name.strip().lower()).strip("-")
    body = dict(payload)
    if run_id is not None:
        body["run_id"] = run_id
        path = outbox_dir / f"autonomous-{run_id}.json"
    else:
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


def test_run_fn_controller_finalizes_repo_tail_without_spending_agent_tick(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("claude")
    calls = []

    def fake_subprocess_run(command):
        calls.append(command)
        return completed(json.dumps({
            "status": "completed", "done": True, "committed": True,
            "clean": True, "tests_passed": True, "pushed": True,
            "commit_hash": "abc123", "remote_commit": "abc123",
        }))

    heads = iter(("before123", "before123", "abc123"))

    def fake_git(_command):
        return completed(next(heads) + "\n")

    project = ProjectRecord(
        name="Demo",
        orchestrator_ready_task="Finalize the repository",
        dod=[
            DoDItem(text="implementation complete", checked=True),
            DoDItem(text="create orchestrator-approved commit"),
            DoDItem(text="po commitu ověřit git status"),
            DoDItem(text="push and verify remote commit"),
        ],
        checkpoint={"completed_dod_indices": [0]},
    )
    run_fn, _, _ = make_run_fn(
        tmp_path, registry, subprocess_run=fake_subprocess_run,
        finalize_command=["controller-finalize"],
        finalize_paths={"Demo": ["tracked.py"]},
        allowed_push_remotes={"Demo": "https://example.invalid/repo.git"},
        run_git=fake_git,
    )

    result = run_fn(project, "claude")

    assert result["status"] == "done"
    assert result["checkpoint"]["completed_dod_indices"] == [0, 1, 2, 3]
    assert len(calls) == 1
    assert calls[0][:3] == ["controller-finalize", "--project", str(tmp_path / "demo-checkout")]
    assert "--push" in calls[0]
    assert calls[0][calls[0].index("--path") + 1] == "tracked.py"
    assert calls[0][calls[0].index("--allowed-remote") + 1] == "https://example.invalid/repo.git"


def test_run_fn_does_not_controller_finalize_fresh_implementation_card(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("claude")
    calls = []

    def fake_subprocess_run(command):
        calls.append(command)
        assert command[0] == "ai-orchestrator"
        run_id = command[command.index("--run-id") + 1]
        write_outbox_result(
            tmp_path / "outbox",
            "Demo",
            {"status": "in_progress", "run_id": run_id},
            run_id=run_id,
        )
        return completed()

    project = ProjectRecord(
        name="Demo",
        orchestrator_ready_task="Install and verify the scheduler",
        dod=[DoDItem(text="install the persistent scheduler")],
    )
    run_fn, _, _ = make_run_fn(
        tmp_path,
        registry,
        subprocess_run=fake_subprocess_run,
        finalize_command=["controller-finalize"],
        finalize_paths={"Demo": ["tracked.py"]},
        allowed_push_remotes={"Demo": "https://example.invalid/repo.git"},
    )

    result = run_fn(project, "claude")

    assert result["status"] == "in_progress"
    assert len(calls) == 1
    assert "--implementation-only" in calls[0]


def test_run_fn_blocks_fresh_card_on_preexisting_dirty_checkout_without_provider_call(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("claude")

    def forbidden_subprocess(_command):
        raise AssertionError("provider must not run over another card's dirty checkout")

    def fake_git(argv):
        if tuple(argv[-2:]) == ("rev-parse", "HEAD"):
            return completed("abc123\n")
        if tuple(argv[-2:]) == ("status", "--porcelain"):
            return completed(" M preceding-card.py\n")
        raise AssertionError(argv)

    project = ProjectRecord(
        name="Demo",
        orchestrator_ready_task="Implement a fresh independent card",
        dod=[DoDItem(text="implementation")],
    )
    run_fn, _, _ = make_run_fn(
        tmp_path,
        registry,
        subprocess_run=forbidden_subprocess,
        run_git=fake_git,
        enforce_clean_preflight=True,
    )

    result = run_fn(project, "claude")

    assert result["status"] == "in_progress"
    assert "without provider call" in result["stop_reason"]


@pytest.mark.parametrize("command", [None, []])
def test_build_finalize_fn_without_command_blocks_dirty_checkout(tmp_path, command):
    """Missing controller wiring must never promote uncommitted agent work."""
    def fake_git(argv):
        if tuple(argv[-2:]) == ("rev-parse", "HEAD"):
            return completed("abc123\n")
        if tuple(argv[-2:]) == ("status", "--porcelain"):
            return completed(" M changed.py\n")
        raise AssertionError(argv)

    finalize_fn = build_finalize_fn(
        command,
        project_paths={"Demo": str(tmp_path / "demo-checkout")},
        run_git=fake_git,
    )

    result = finalize_fn(ProjectRecord(name="Demo"))

    assert result["status"] == "blocked"
    assert "not configured" in result["stop_reason"]


def test_build_finalize_fn_without_command_allows_clean_checkout(tmp_path):
    """No finalizer process is needed when the card produced no Git change."""
    def fake_git(argv):
        if tuple(argv[-2:]) == ("rev-parse", "HEAD"):
            return completed("abc123\n")
        if tuple(argv[-2:]) == ("status", "--porcelain"):
            return completed("")
        raise AssertionError(argv)

    finalize_fn = build_finalize_fn(
        None,
        project_paths={"Demo": str(tmp_path / "demo-checkout")},
        run_git=fake_git,
    )

    assert finalize_fn(ProjectRecord(name="Demo")) == {
        "status": "done",
        "already_verified": True,
    }


def test_build_finalize_fn_commits_a_fully_implemented_card(tmp_path):
    """This is the automatic path daemon._promote_completed_implementations_
    to_testing calls right before promoting Pracuje se -> Testování - it must
    actually run the controller finalizer and return its verified proof, not
    just describe what a caller should do."""
    registry_calls = []

    def fake_subprocess_run(command):
        registry_calls.append(command)
        return completed(json.dumps({
            "status": "completed", "done": True, "committed": True,
            "clean": True, "tests_passed": True, "pushed": True,
            "commit_hash": "abc123", "remote_commit": "abc123",
        }))

    heads = iter(("before123", "before123", "abc123"))

    def fake_git(_command):
        return completed(next(heads) + "\n")

    project = ProjectRecord(
        name="Demo",
        dod=[DoDItem(text="implementation complete", checked=True)],
        checkpoint={"completed_dod_indices": [0]},
    )
    finalize_fn = build_finalize_fn(
        ["controller-finalize"],
        project_paths={"Demo": str(tmp_path / "demo-checkout")},
        finalize_paths={"Demo": ["tracked.py"]},
        allowed_push_remotes={"Demo": "https://example.invalid/repo.git"},
        subprocess_run=fake_subprocess_run,
        run_git=fake_git,
        run_id_fn=lambda: "fixed-run-id",
    )

    result = finalize_fn(project)

    assert result["status"] == "done"
    assert result["checkpoint"]["finalization"]["commit_hash"] == "abc123"
    assert len(registry_calls) == 1
    assert "--push" in registry_calls[0]
    assert registry_calls[0][registry_calls[0].index("--path") + 1] == "tracked.py"


def test_build_finalize_fn_skips_an_already_verified_checkout(tmp_path):
    """A card already finalized for the current HEAD (a research-only card
    with nothing to commit, or a retry after a transient Trello write
    failure) must not spend a second finalizer subprocess call."""
    def fake_subprocess_run(_command):
        raise AssertionError("must not re-run the finalizer for a verified HEAD")

    def fake_git(_command):
        return completed("abc123\n")

    project = ProjectRecord(
        name="Demo",
        dod=[DoDItem(text="implementation complete", checked=True)],
        checkpoint={
            "finalization": {
                "status": "completed", "done": True, "committed": True,
                "clean": True, "tests_passed": True, "pushed": True,
                "commit_hash": "abc123", "remote_commit": "abc123",
            },
        },
    )
    finalize_fn = build_finalize_fn(
        ["controller-finalize"],
        project_paths={"Demo": str(tmp_path / "demo-checkout")},
        subprocess_run=fake_subprocess_run,
        run_git=fake_git,
    )

    result = finalize_fn(project)

    assert result == {"status": "done", "already_verified": True}


def test_finalization_refresh_is_needed_when_card_proof_has_old_head(tmp_path):
    project = ProjectRecord(
        name="Demo",
        checkpoint={"finalization": {"commit_hash": "old-head"}},
    )

    def fake_git(_command):
        return completed("new-head\n")

    assert _finalization_needs_refresh(project, str(tmp_path / "demo-checkout"), fake_git) is True


def test_controller_finalization_accepts_clean_refresh_without_second_commit():
    head = "9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b"
    finalization = {
        "status": "completed",
        "done": True,
        "committed": False,
        "clean": True,
        "tests_passed": True,
        "pushed": True,
        "commit_hash": head,
        "remote_commit": head,
    }

    assert _controller_finalization_is_verified(finalization, head) is True


@pytest.mark.parametrize(
    "missing_or_false",
    ["tests_passed", "clean", "pushed", "remote_commit"],
)
def test_controller_finalization_rejects_incomplete_proof(missing_or_false):
    head = "9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b"
    finalization = {
        "status": "completed",
        "done": True,
        "committed": False,
        "clean": True,
        "tests_passed": True,
        "pushed": True,
        "commit_hash": head,
        "remote_commit": head,
    }
    if missing_or_false == "remote_commit":
        finalization.pop(missing_or_false)
    else:
        finalization[missing_or_false] = False

    assert _controller_finalization_is_verified(finalization, head) is False


def test_controller_finalization_rejects_proof_for_different_actual_head():
    proof_head = "9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b"
    finalization = {
        "status": "completed",
        "done": True,
        "committed": True,
        "clean": True,
        "tests_passed": True,
        "pushed": True,
        "commit_hash": proof_head,
        "remote_commit": proof_head,
    }

    assert _controller_finalization_is_verified(finalization, "different-actual-head") is False


def test_controller_finalization_rejects_new_commit_claim_without_head_change():
    head = "9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b"
    finalization = {
        "status": "completed",
        "done": True,
        "committed": True,
        "clean": True,
        "tests_passed": True,
        "pushed": True,
        "commit_hash": head,
        "remote_commit": head,
    }

    assert _controller_finalization_is_verified(
        finalization, head, previous_head=head
    ) is False


def test_controller_finalization_rejects_noop_claim_when_head_changed():
    old_head = "1" * 40
    new_head = "2" * 40
    finalization = {
        "status": "completed",
        "done": True,
        "committed": False,
        "clean": True,
        "tests_passed": True,
        "pushed": True,
        "commit_hash": new_head,
        "remote_commit": new_head,
    }

    assert _controller_finalization_is_verified(
        finalization, new_head, previous_head=old_head
    ) is False


def test_terminal_gate_rejects_dirty_checkout_without_controller_proof(tmp_path):
    def fake_git(command):
        if "status" in command:
            return completed(" M source.py\n")
        return completed("head\n")

    issue = _terminal_finalization_issue(
        None, "head", str(tmp_path / "demo-checkout"), fake_git
    )

    assert issue is not None
    assert "controller finalizace" in issue
    assert "dirty" in issue


def test_terminal_gate_allows_clean_checkout_without_noop_commit(tmp_path):
    def fake_git(command):
        if "status" in command:
            return completed("")
        return completed("head\n")

    assert _terminal_finalization_issue(
        None, "head", str(tmp_path / "demo-checkout"), fake_git
    ) is None


def test_audit_run_fn_uses_supported_autonomous_cli_and_reads_internal_audit(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("claude")
    registry.configure_models("claude", ["claude-opus-4-1"])
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
                "active_provider": "anthropic",
                "active_model": "claude-opus-4-1",
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
    assert seen["command"][seen["command"].index("--model") + 1] == "claude-opus-4-1"
    assert seen["command"][seen["command"].index("--max-iterations") + 1] == "1"
    assert seen["command"][-1] == "--no-commit"
    assert result["active_provider"] == "anthropic"
    assert result["active_model"] == "claude-opus-4-1"


def test_audit_and_implementation_request_models_by_task_classification(tmp_path):
    """A verified catalog produces distinct concrete requests by task type."""
    registry = ProviderRegistry()
    registry.mark_available("claude")
    registry.configure_models("claude", ["claude-sonnet-4", "claude-opus-4-1"])

    implementation_project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.READY,
        orchestrator_ready_task="Implement feature X",
    )
    implementation_seen = {}

    def fake_impl_subprocess_run(command):
        implementation_seen["command"] = command
        write_outbox_result(
            tmp_path / "outbox", "Demo",
            {"checkpoint": {}, "status": "in_progress"},
            run_id="fixed-run-id",
        )
        return completed()

    run_fn, _, _ = make_run_fn(tmp_path, registry, subprocess_run=fake_impl_subprocess_run)
    run_fn(implementation_project, "claude")
    assert implementation_seen["command"][implementation_seen["command"].index("--model") + 1] == "claude-sonnet-4"

    audit_project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.TESTING,
        orchestrator_ready_task="Verify the feature",
        dod=[DoDItem(text="implemented", checked=True)],
        checkpoint={"completed_dod_indices": [0]},
    )
    audit_seen = {}

    def fake_audit_subprocess_run(command):
        audit_seen["command"] = command
        write_outbox_result(
            tmp_path / "outbox", "Demo",
            {"status": "completed", "last_output": "audit passed", "iterations": [{
                "audit_performed": True, "audit_rejected_indices": [], "audit_protocol_error": False,
                "test_output": "1 passed",
            }]},
            run_id="audit-run",
        )
        return completed()

    build_audit_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={"Demo": str(tmp_path / "demo-checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(tmp_path / "outbox"),
        subprocess_run=fake_audit_subprocess_run,
        run_id_fn=lambda: "audit-run",
    )(audit_project, "claude")

    assert audit_seen["command"][audit_seen["command"].index("--model") + 1] == "claude-opus-4-1"


def test_production_audit_starts_with_pm_selected_provider_and_skips_capability_limited_provider(tmp_path):
    registry = ProviderRegistry()
    for name in ("antigravity", "claude", "codex"):
        registry.mark_available(name)
    registry.mark_capability_limited(
        "antigravity", "audit:station agent:propagation a scoring", "review plan without verdict"
    )
    project = ProjectRecord(
        name="Station audit", project_key="Station Agent", status=ProjectStatus.TESTING,
        orchestrator_ready_task="Verify propagation a scoring",
        dod=[DoDItem(text="implemented", checked=True)],
        extra_data={"inbox_preparation": {"scope": "propagation a scoring"}},
    )
    seen = {}

    def fake_subprocess_run(command):
        seen["command"] = command
        write_outbox_result(
            tmp_path / "outbox", "Station audit",
            {"status": "completed", "iterations": [{
                "audit_performed": True, "audit_rejected_indices": [],
                "audit_protocol_error": False, "test_output": "1 passed",
            }]},
            run_id="audit-run",
        )
        return completed()

    result = build_audit_run_fn(
        registry, command=["ai-orchestrator"],
        project_paths={"Station Agent": str(tmp_path / "demo-checkout")},
        spec_dir=str(tmp_path / "specs"), outbox_dir=str(tmp_path / "outbox"),
        subprocess_run=fake_subprocess_run, run_id_fn=lambda: "audit-run",
        use_provider_failover=True,
    )(project, "codex")

    assert result["verdict"] == "accepted"
    assert seen["command"][seen["command"].index("--agent") + 1] == "auto"
    assert seen["command"][seen["command"].index("--provider-order") + 1] == (
        "codex,claude-code"
    )


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
    assert result["rejected_indices"] == [0]


def test_audit_run_fn_routes_audit_only_rejection_back_to_testing(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("claude")
    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.TESTING,
        orchestrator_ready_task="Verify the live endpoint",
        dod=[
            DoDItem(text="implemented", checked=True),
            DoDItem(text="independent audit", phase="audit", checked=False),
        ],
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
                    "audit_rejected_indices": [1],
                    "audit_protocol_error": False,
                    "note": "live endpoint evidence is missing",
                    "test_output": "1 passed",
                }],
            },
            run_id="audit-only-run",
        )
        return completed()

    result = build_audit_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={"Demo": str(tmp_path / "demo-checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(tmp_path / "outbox"),
        subprocess_run=fake_subprocess_run,
        run_id_fn=lambda: "audit-only-run",
    )(project, "claude")

    assert result["verdict"] == "rejected"
    assert result["reject_target"] == "testing"
    assert result["rejected_indices"] == [1]


# ---- resolving a project's local path (item 1) ------------------------

def test_resolve_project_path_uses_explicit_per_project_override():
    project = ProjectRecord(name="Demo", project_key="Demo")
    path = resolve_project_path(project, project_paths={"Demo": "/checkouts/demo"})
    assert path == "/checkouts/demo"


def test_resolve_project_path_does_not_fall_back_to_slugified_shared_root():
    project = ProjectRecord(name="My Cool Project")
    with pytest.raises(ProjectPathError, match="no project identity label"):
        resolve_project_path(project, projects_root="/work")


def test_resolve_project_path_raises_without_any_mapping_configured():
    project = ProjectRecord(name="Demo")
    with pytest.raises(ProjectPathError):
        resolve_project_path(project)


def test_only_p5_without_project_label_fails_closed():
    project = ProjectRecord(name="P5 - generic task", priority=5, project_key=None)
    with pytest.raises(ProjectPathError, match="P0-P5 is priority only"):
        resolve_project_path(
            project,
            project_paths={"AI Project Manager": r"D:\orchestrator\ai-project-manager"},
            projects_root=r"D:\orchestrator",
        )


@pytest.mark.parametrize(
    "identity,expected",
    [
        ("AI Project Manager", r"D:\orchestrator\ai-project-manager"),
        ("AI Orchestrator", r"D:\orchestrator\ai-orchestrator"),
        ("Station Agent", r"D:\orchestrator\station-agent"),
    ],
)
def test_project_identity_maps_only_to_its_allowlisted_repo(identity, expected):
    paths = {
        "AI Project Manager": r"D:\orchestrator\ai-project-manager",
        "AI Orchestrator": r"D:\orchestrator\ai-orchestrator",
        "Station Agent": r"D:\orchestrator\station-agent",
    }
    project = ProjectRecord(name="P5 - generic task", priority=5, project_key=identity)
    assert resolve_project_path(project, project_paths=paths) == expected


def test_resolve_project_path_never_uses_title_override():
    project = ProjectRecord(
        name="P5 - Station Agent", project_key="Station Agent"
    )
    path = resolve_project_path(
        project,
        project_paths={
            "Station Agent": "/checkouts/station-agent",
            "P5 - Station Agent": "/checkouts/title-override",
        },
    )
    assert path == "/checkouts/station-agent"


def test_resolve_project_path_rejects_conflicting_normalized_identity_mappings():
    project = ProjectRecord(name="P3 - Foo", project_key="Foo")
    with pytest.raises(ProjectPathError, match="configured paths disagree"):
        resolve_project_path(
            project,
            project_paths={
                "Foo": "/checkouts/foo-repo",
                " foo ": "/checkouts/other-repo",
            },
        )


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


def test_resolve_project_path_without_project_key_never_uses_title_phrase():
    project = ProjectRecord(name="P5 - Station Agent", priority=5)
    with pytest.raises(ProjectPathError, match="no project identity label"):
        resolve_project_path(project, project_paths=_STABLE_IDENTITY_PROJECT_PATHS)


def test_resolve_project_path_project_key_wins_over_exact_title_entry():
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
    assert path == "/checkouts/ai-project-manager"


def test_resolve_project_path_distinguishes_similarly_prefixed_projects():
    project_paths = {
        "Řídicí systém": "/checkouts/ai-project-manager",
        "AI Project Manager": "/checkouts/ai-project-manager",
        "ai-orchestrator": "/checkouts/ai-orchestrator",
    }
    pm_project = ProjectRecord(
        name="P1 - Audit a stabilizace AI Project Manager",
        project_key="AI Project Manager",
    )
    orch_project = ProjectRecord(
        name="P1 - Audit a stabilizace ai-orchestrator",
        project_key="ai-orchestrator",
    )
    assert resolve_project_path(pm_project, project_paths=project_paths) == "/checkouts/ai-project-manager"
    assert resolve_project_path(orch_project, project_paths=project_paths) == "/checkouts/ai-orchestrator"


# ---- the real CLI argument shape (item 0 / regression test, item 6) ---

def test_run_fn_invokes_real_cli_with_project_goal_spec_and_agent(tmp_path):
    seen = {}
    registry = ProviderRegistry()
    registry.mark_available("claude")
    registry.configure_models("claude", ["claude-opus-4-1"])

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
    assert command[command.index("--model") + 1] == "claude-opus-4-1"
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


def test_production_run_fn_dispatches_auto_for_same_tick_provider_failover(tmp_path):
    seen = {}
    registry = ProviderRegistry()
    for name in ("antigravity", "claude", "codex"):
        registry.mark_available(name)
    registry.configure_models("antigravity", ["fast-model", "quality-model"])

    def fake_subprocess_run(command):
        seen["command"] = command
        write_outbox_result(
            tmp_path / "outbox", "Demo",
            {"status": "in_progress", "checkpoint": {}, "provider_sequence": ["antigravity"]},
            run_id="fixed-run-id",
        )
        return completed()

    project = ProjectRecord(
        name="Demo", status=ProjectStatus.READY,
        orchestrator_ready_task="Implement feature X",
    )
    run_fn, _, _ = make_run_fn(
        tmp_path, registry, subprocess_run=fake_subprocess_run,
        use_provider_failover=True,
    )

    run_fn(project, "antigravity")

    assert seen["command"][seen["command"].index("--agent") + 1] == "auto"
    assert seen["command"][seen["command"].index("--provider-order") + 1] == "antigravity,claude-code,codex"
    assert "gemini" not in seen["command"][seen["command"].index("--provider-order") + 1]
    assert "--model" not in seen["command"]
    assert run_fn.select_model(project, "antigravity") is None


def test_auto_provider_alias_resolves_to_real_available_failover_order(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("auto")
    registry.mark_limited("antigravity", timedelta(hours=1))
    registry.mark_available("claude")
    registry.mark_available("codex")

    assert _tick_provider_order("auto", registry) == ["claude-code", "codex"]

    seen = {}

    def fake_subprocess_run(command):
        seen["command"] = command
        write_outbox_result(
            tmp_path / "outbox",
            "Demo",
            {"status": "in_progress", "provider_sequence": ["claude-code"]},
            run_id="auto-run",
        )
        return completed()

    project = ProjectRecord(name="Demo", status=ProjectStatus.READY, orchestrator_ready_task="Implement")
    run_fn, _, _ = make_run_fn(
        tmp_path,
        registry,
        subprocess_run=fake_subprocess_run,
        run_id_fn=lambda: "auto-run",
        use_provider_failover=True,
    )

    run_fn(project, "auto")

    assert seen["command"][seen["command"].index("--agent") + 1] == "auto"
    assert seen["command"][seen["command"].index("--provider-order") + 1] == (
        "claude-code,codex"
    )


def test_production_failover_suppresses_pm_selected_model(tmp_path):
    # Same-tick failover hands the whole provider chain to ai-orchestrator,
    # so a single PM-selected model can never apply across it - this must
    # A single PM-selected model must never apply across an AO failover chain.
    registry = ProviderRegistry()
    for name in ("antigravity", "claude", "codex"):
        registry.mark_available(name)
    registry.configure_models("codex", ["gpt-5.6", "gpt-5.4"])
    seen = {}

    def fake_subprocess_run(command):
        seen["command"] = command
        write_outbox_result(
            tmp_path / "outbox", "Demo",
            {"status": "in_progress", "active_provider": "codex", "active_model": "gpt-5.6"},
            run_id="model-run",
        )
        return completed()

    project = ProjectRecord(name="Demo", project_key="Demo", orchestrator_ready_task="Implement")
    build_run_fn(
        registry,
        command=["ai-orchestrator", "autonomous"],
        project_paths={"Demo": str(tmp_path / "demo-checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(tmp_path / "outbox"),
        subprocess_run=fake_subprocess_run,
        run_id_fn=lambda: "model-run",
        use_provider_failover=True,
    )(project, "codex")

    assert seen["command"][seen["command"].index("--agent") + 1] == "auto"
    assert "--model" not in seen["command"]


def test_production_failover_temporarily_gates_failed_head_provider(tmp_path):
    # Generalizes the retired Hermes-only cooldown (see git history) to
    # every provider in the post-Hermes routing: a head provider that fails
    # over to a later one in the same AO run must not be immediately
    # re-tried by the next PM tick before a cooldown backoff elapses.
    registry = ProviderRegistry()
    for name in ("antigravity", "claude", "codex"):
        registry.mark_available(name)

    def fake_subprocess_run(command):
        write_outbox_result(
            tmp_path / "outbox", "Demo",
            {
                "status": "in_progress",
                "checkpoint": {"completed_dod_indices": []},
                "active_provider": "codex",
                "provider_sequence": ["antigravity", "claude-code", "codex"],
                "stop_reason": "pokračování přes codex",
            },
            run_id="fixed-run-id",
        )
        return completed()

    project = ProjectRecord(
        name="Demo", status=ProjectStatus.READY,
        orchestrator_ready_task="Implement feature X",
    )
    run_fn, _, _ = make_run_fn(
        tmp_path, registry, subprocess_run=fake_subprocess_run,
        use_provider_failover=True,
    )

    run_fn(project, "antigravity")

    failed_status = registry.get_status("antigravity")
    assert failed_status.state == ProviderState.ERROR
    assert failed_status.retry_after is not None
    assert "failover" in failed_status.last_error

    also_failed_status = registry.get_status("claude")
    assert also_failed_status.state == ProviderState.ERROR
    assert also_failed_status.retry_after is not None

    # The provider that actually completed the run must remain untouched.
    assert registry.get_status("codex").state == ProviderState.AVAILABLE


def test_production_failover_excludes_limited_providers_from_next_workflow_step(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("antigravity")
    registry.mark_limited("claude", timedelta(minutes=30))
    registry.mark_available("codex")
    seen = {}

    def fake_subprocess_run(command):
        seen["command"] = command
        write_outbox_result(
            tmp_path / "outbox",
            "Demo",
            {"status": "in_progress", "provider_sequence": ["codex"]},
            run_id="fixed-run-id",
        )
        return completed()

    project = ProjectRecord(
        name="Demo", status=ProjectStatus.READY,
        orchestrator_ready_task="Implement feature X",
    )
    run_fn, _, _ = make_run_fn(
        tmp_path, registry, subprocess_run=fake_subprocess_run,
        use_provider_failover=True,
    )

    run_fn(project, "antigravity")

    order = seen["command"][seen["command"].index("--provider-order") + 1]
    assert order == "antigravity,codex"
    assert "claude-code" not in order


def test_production_waiting_receipt_gates_all_limited_providers_and_preserves_retry_at(tmp_path):
    registry = ProviderRegistry()
    for name in ("antigravity", "claude", "codex"):
        registry.mark_available(name)

    def fake_subprocess_run(command):
        write_outbox_result(
            tmp_path / "outbox",
            "Demo",
            {
                "status": "waiting_for_provider",
                "error": "all providers limited",
                "retry_after_seconds": 30,
                "active_provider": "codex",
                "provider_statuses": {
                    "antigravity": {
                        "state": "LIMITED",
                        "retry_after_seconds": 120,
                        "retry_at": "2026-09-02T11:02:00+00:00",
                        "reason": "RESOURCE_EXHAUSTED",
                    },
                    "claude-code": {
                        "state": "LIMITED",
                        "retry_after_seconds": 900,
                        "retry_at": "2026-09-02T11:15:00+00:00",
                        "reason": "session limit",
                    },
                    "codex": {
                        "state": "LIMITED",
                        "retry_after_seconds": 30,
                        "retry_at": "2026-09-02T11:00:30+00:00",
                        "reason": "rate limit",
                    },
                },
            },
            run_id="fixed-run-id",
        )
        return completed()

    project = ProjectRecord(
        name="Demo", status=ProjectStatus.READY,
        orchestrator_ready_task="Implement feature X",
    )
    run_fn, _, _ = make_run_fn(
        tmp_path, registry, subprocess_run=fake_subprocess_run,
        use_provider_failover=True,
    )

    result = run_fn(project, "antigravity")

    assert result["status"] == "paused"
    assert registry.get_status("antigravity").retry_after.isoformat() == "2026-09-02T11:02:00+00:00"
    assert registry.get_status("claude").retry_after.isoformat() == "2026-09-02T11:15:00+00:00"
    assert registry.get_status("codex").retry_after.isoformat() == "2026-09-02T11:00:30+00:00"
    assert all(not registry.is_available(name) for name in ("antigravity", "claude", "codex"))


def test_run_fn_preserves_model_identity_from_orchestrator_receipt(tmp_path):
    registry = ProviderRegistry()
    registry.mark_available("codex")
    registry.configure_models("codex", ["gpt-5.6"])

    def fake_subprocess_run(command):
        write_outbox_result(
            tmp_path / "outbox",
            "Demo",
            {
                "status": "in_progress",
                "active_provider": "openai",
                "active_model": "gpt-5.6-codex",
            },
            run_id="fixed-run-id",
        )
        return completed()

    project = ProjectRecord(name="Demo", orchestrator_ready_task="Implement feature X")
    run_fn, _, _ = make_run_fn(tmp_path, registry, subprocess_run=fake_subprocess_run)

    result = run_fn(project, "codex")

    assert result["active_provider"] == "openai"
    assert result["active_model"] == "gpt-5.6-codex"


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


def test_audit_wait_preserves_actual_failover_provider_from_outbox(tmp_path):
    def fake_subprocess_run(command):
        write_outbox_result(
            tmp_path / "outbox",
            "Demo",
            {
                "status": "waiting_for_provider",
                "error": "all providers limited",
                "retry_after_seconds": 120,
                "active_provider": "codex",
                "provider_sequence": ["codex"],
                "checkpoint": {"completed_dod_indices": [0]},
            },
            run_id="audit-wait-run",
        )
        return completed()

    project = ProjectRecord(
        name="Demo",
        status=ProjectStatus.TESTING,
        main_task="Audit the implementation",
        dod=[DoDItem(text="implemented", checked=True)],
    )
    registry = ProviderRegistry()
    registry.mark_available("antigravity")
    audit_fn = build_audit_run_fn(
        registry,
        command=["ai-orchestrator", "autonomous"],
        project_paths={"Demo": str(tmp_path / "demo-checkout")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(tmp_path / "outbox"),
        subprocess_run=fake_subprocess_run,
        run_id_fn=lambda: "audit-wait-run",
        use_provider_failover=True,
    )

    result = audit_fn(project, "antigravity")

    assert result["status"] == "paused"
    assert result["active_provider"] == "codex"
    assert result["provider_sequence"] == ["codex"]
    assert registry.get_status("codex").state == ProviderState.LIMITED


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


def test_inbox_planner_excludes_retired_provider_names_and_returns_validated_ai_tasks():
    registry = ProviderRegistry()
    for name in ("hermes", "gemini", "antigravity", "codex"):
        registry.mark_available(name)
    registry.configure_models("antigravity", ["Gemini 3.7 Flash (High)"])
    calls = []
    selections = []

    def fake_subprocess(command, **kwargs):
        calls.append((command, kwargs))
        return completed(
            json.dumps(
                {
                    "success": True,
                    "provider": "antigravity",
                    "model": "Gemini 3.7 Flash (High)",
                    "output": json.dumps(
                        {
                            "tasks": [
                                {
                                    "project_key": "AI Project Manager",
                                    "scope": "regrese",
                                    "task": "Opravit potvrzenou regresi.",
                                    "next_step": "Reprodukovat regresi.",
                                    "priority": 4.01,
                                    "priority_reason": "potvrzená regrese; P5 pracovní oprava",
                                    "work_type": "implementation",
                                    "split_reason": "Jeden koherentní výsledek opravy.",
                                }
                            ]
                        }
                    ),
                }
            )
        )

    planner = build_inbox_planner_fn(
        registry,
        ["python", "orchestrator.py", "autonomous", "--no-commit"],
        project_paths={
            "AI Project Manager": r"D:\orchestrator\ai-project-manager",
            "AI Orchestrator": r"D:\orchestrator\ai-orchestrator",
        },
        subprocess_run=fake_subprocess,
        selection_notifier=selections.append,
    )
    result = planner({"id": "source", "name": "Regrese", "desc": "Opravit regresi"}, [])

    assert result["provider"] == "antigravity"
    assert result["model"] == "Gemini 3.7 Flash (High)"
    assert "první dostupný provider" in result["provider_reason"]
    assert "skutečně použitý model" in result["model_reason"]
    assert selections[0]["provider"] == "antigravity"
    assert selections[0]["selected_model"] == "Gemini 3.7 Flash (High)"
    assert selections[0]["model"] is None
    assert selections[0]["task_type"] == "inbox_planning"
    assert result["tasks"][0].priority == 4.01
    assert calls[0][0] == [
        "python", "orchestrator.py", "plan-inbox", "--agent", "antigravity",
        "--model", "Gemini 3.7 Flash (High)",
    ]
    assert "gemini" not in calls[0][0]
    assert "hermes" not in calls[0][0]
    planner_request = json.loads(calls[0][1]["input"])
    assert planner_request["configured_projects"] == [
        {
            "project_key": "AI Project Manager",
            "project_path": r"D:\orchestrator\ai-project-manager",
        },
        {
            "project_key": "AI Orchestrator",
            "project_path": r"D:\orchestrator\ai-orchestrator",
        },
    ]


def _two_task_subprocess(command, **kwargs):
    return completed(
        json.dumps(
            {
                "success": True,
                "provider": "claude-code",
                "model": "claude-haiku",
                "output": json.dumps(
                    {
                        "tasks": [
                            {
                                "scope": "část 1",
                                "task": "Udělat první část.",
                                "next_step": "Začít.",
                                "priority": 2,
                                "priority_reason": "výchozí priorita",
                                "work_type": "implementation",
                                "split_reason": "Samostatný výsledek první části.",
                            },
                            {
                                "scope": "část 2",
                                "task": "Udělat druhou část.",
                                "next_step": "Pokračovat.",
                                "priority": 3,
                                "priority_reason": "výchozí priorita",
                                "work_type": "implementation",
                                "split_reason": "Samostatný výsledek druhé části.",
                            },
                        ]
                    }
                ),
            }
        )
    )


def test_inbox_planner_fails_closed_on_multi_task_plan_when_source_explicitly_marked_indivisible():
    """A source card carrying the explicit ``[indivisible]`` marker is bound
    to exactly one AI-planned task; a multi-task plan must be rejected
    before it can reach ``process_inbox`` and be materialized into
    Připraveno."""
    registry = ProviderRegistry()
    registry.mark_available("claude-code")

    planner = build_inbox_planner_fn(
        registry,
        ["python", "orchestrator.py", "autonomous", "--no-commit"],
        subprocess_run=_two_task_subprocess,
    )

    assert planner({"id": "source", "name": "Nápad [indivisible]", "desc": "Úkol"}, []) is None
    status = registry.get_status("claude-code")
    assert "nedělitelná" in status.last_error


def test_inbox_planner_allows_multi_task_plan_for_ordinary_splittable_source():
    """A source card without the explicit ``[indivisible]`` marker is an
    ordinary splittable request: the AI planner may describe several
    tasks for it."""
    registry = ProviderRegistry()
    registry.mark_available("claude-code")

    planner = build_inbox_planner_fn(
        registry,
        ["python", "orchestrator.py", "autonomous", "--no-commit"],
        subprocess_run=_two_task_subprocess,
    )

    result = planner({"id": "source", "name": "Nápad", "desc": "Úkol"}, [])
    assert result is not None
    assert len(result["tasks"]) == 2


def test_inbox_planner_does_not_fallback_to_retired_provider_when_it_is_the_only_provider():
    registry = ProviderRegistry()
    registry.mark_available("hermes")
    calls = []

    def fake_subprocess(command, **kwargs):
        calls.append(command)
        return completed()

    planner = build_inbox_planner_fn(
        registry,
        ["python", "orchestrator.py", "autonomous", "--no-commit"],
        subprocess_run=fake_subprocess,
    )

    assert planner({"id": "source", "name": "Nápad", "desc": "Úkol"}, []) is None
    assert calls == []


def test_inbox_planner_requires_an_explainable_priority_reason():
    registry = ProviderRegistry()
    registry.mark_available("claude")

    def fake_subprocess(command, **kwargs):
        return completed(json.dumps({
            "success": True,
            "provider": "claude",
            "model": "claude-haiku",
            "output": json.dumps({
                "tasks": [{
                    "scope": "feature",
                    "task": "Vytvořit funkci.",
                    "next_step": "Navrhnout rozhraní.",
                    "priority": 5.7,
                    "depends_on": [],
                }],
            }),
        }))

    planner = build_inbox_planner_fn(
        registry,
        ["python", "orchestrator.py", "autonomous", "--no-commit"],
        subprocess_run=fake_subprocess,
    )

    assert planner({"id": "source", "name": "Nápad", "desc": "Úkol"}, []) is None


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


# ---- P5 false completion regression (item 7) -------------------------

def test_p5_false_completion_regression_audit_rejects_dirty_unchanged_head_empty_remote(tmp_path):
    """Regression test for P5 false completion bug:
    P5 'AO: cleanup, commit a záloha po integraci providera' was marked Hotovo
    despite ai-orchestrator being dirty, HEAD remaining 4f9c001 (no new commit),
    and git remote -v being empty.
    With fail-closed validation, the audit MUST reject this run and refuse
    transition to Hotovo.
    """
    (tmp_path / "ai-orchestrator").mkdir()
    registry = ProviderRegistry()
    registry.mark_available("claude")
    project = ProjectRecord(
        name="P5 — AO: cleanup, commit a záloha po integraci providera",
        status=ProjectStatus.TESTING,
        project_key="AI Orchestrator",
        main_task="AO: cleanup, commit a záloha po integraci providera",
        dod=[DoDItem(text="AO: cleanup, commit a záloha po integraci providera", checked=False)],
        checkpoint={"completed_dod_indices": [0]},
    )

    def fake_git(command, cwd=None):
        subcmd = command[3] if len(command) > 3 and command[1] == "-C" else command[1] if len(command) > 1 else ""
        if subcmd == "rev-parse":
            return subprocess.CompletedProcess(args=list(command), returncode=0, stdout="4f9c001\n", stderr="")
        if subcmd == "status":
            return subprocess.CompletedProcess(args=list(command), returncode=0, stdout="M provider_adapter.py\n?? scratch.py\n", stderr="")
        if subcmd == "diff":
            return subprocess.CompletedProcess(args=list(command), returncode=0, stdout="diff content\n", stderr="")
        if subcmd == "remote":
            return subprocess.CompletedProcess(args=list(command), returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=list(command), returncode=0, stdout="", stderr="")

    def fake_subprocess_run(command):
        write_outbox_result(
            tmp_path / "outbox",
            project.name,
            {
                "status": "completed",
                "last_output": "cleanup, commit a záloha hotova",
                "iterations": [{
                    "audit_performed": True,
                    "audit_rejected_indices": [],
                    "audit_protocol_error": False,
                    "test_output": "5 passed",
                }],
            },
            run_id="p5-audit-run",
        )
        return completed()

    audit_run_fn = build_audit_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={"AI Orchestrator": str(tmp_path / "ai-orchestrator")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(tmp_path / "outbox"),
        subprocess_run=fake_subprocess_run,
        run_id_fn=lambda: "p5-audit-run",
        run_git=fake_git,
    )

    result = audit_run_fn(project, "claude")

    assert result["verdict"] == "rejected"
    assert "0" in result["reason"]
    assert "dirty" in result["reason"]
    assert "4f9c001" in result["reason"]
    assert "remote" in result["reason"]
    assert result["reject_target"] == "in_progress"


def test_p5_false_completion_regression_run_once_audit_never_moves_to_done(tmp_path):
    """End-to-end regression: run_once_audit with failing DoD git verification
    leaves the card in IN_PROGRESS (Pracuje se) and never moves it to Hotovo (DONE).
    """
    (tmp_path / "ai-orchestrator").mkdir()
    from ai_project_manager.runner import run_once_audit
    from ai_project_manager.trello_client import InMemoryTrelloClient
    from ai_project_manager.trello_sync import build_list_maps, project_from_card, sync_project_to_trello

    project = ProjectRecord(
        name="P5 — AO: cleanup, commit a záloha po integraci providera",
        priority=5,
        status=ProjectStatus.TESTING,
        project_key="AI Orchestrator",
        main_task="AO: cleanup, commit a záloha po integraci providera",
        dod=[DoDItem(text="AO: cleanup, commit a záloha po integraci providera", checked=False)],
    )
    client = InMemoryTrelloClient()
    created = sync_project_to_trello(client, project)
    project.trello_card_id = created["id"]

    registry = ProviderRegistry()
    registry.mark_available("claude")

    def fake_git(command, cwd=None):
        subcmd = command[3] if len(command) > 3 and command[1] == "-C" else command[1] if len(command) > 1 else ""
        if subcmd == "rev-parse":
            return subprocess.CompletedProcess(args=list(command), returncode=0, stdout="4f9c001\n", stderr="")
        if subcmd == "status":
            return subprocess.CompletedProcess(args=list(command), returncode=0, stdout="M provider_adapter.py\n", stderr="")
        if subcmd == "diff":
            return subprocess.CompletedProcess(args=list(command), returncode=0, stdout="diff content\n", stderr="")
        if subcmd == "remote":
            return subprocess.CompletedProcess(args=list(command), returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=list(command), returncode=0, stdout="", stderr="")

    def fake_subprocess_run(command):
        write_outbox_result(
            tmp_path / "outbox",
            project.name,
            {
                "status": "completed",
                "last_output": "cleanup hotov",
                "iterations": [{
                    "audit_performed": True,
                    "audit_rejected_indices": [],
                    "audit_protocol_error": False,
                    "test_output": "5 passed",
                }],
            },
            run_id="p5-audit-run",
        )
        return completed()

    audit_run_fn = build_audit_run_fn(
        registry,
        command=["ai-orchestrator"],
        project_paths={"AI Orchestrator": str(tmp_path / "ai-orchestrator")},
        spec_dir=str(tmp_path / "specs"),
        outbox_dir=str(tmp_path / "outbox"),
        subprocess_run=fake_subprocess_run,
        run_id_fn=lambda: "p5-audit-run",
        run_git=fake_git,
    )

    outcome = run_once_audit(
        client,
        [project],
        registry,
        audit_run_fn,
        default_providers=["claude"],
    )

    assert outcome.ran is True
    assert project.status == ProjectStatus.IN_PROGRESS
    assert project.dod[0].checked is False

    id_to_name, _ = build_list_maps(client)
    reloaded = project_from_card(client.get_card(project.trello_card_id), id_to_name)
    assert reloaded.status == ProjectStatus.IN_PROGRESS
    assert reloaded.status != ProjectStatus.DONE
    assert reloaded.dod[0].checked is False
