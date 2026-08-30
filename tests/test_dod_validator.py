import subprocess
import pytest

from ai_project_manager.dod_validator import (
    get_git_diff,
    get_git_head,
    get_git_remotes,
    get_git_status,
    validate_dod_item,
    validate_project_dod,
)
from ai_project_manager.models import DoDItem, ProjectRecord


def fake_git(responses):
    def run_git(command, cwd=None):
        cmd_key = tuple(command)
        if cmd_key in responses:
            res = responses[cmd_key]
            if isinstance(res, tuple):
                code, out, err = res
            else:
                code, out, err = 0, res, ""
            return subprocess.CompletedProcess(args=list(command), returncode=code, stdout=out, stderr=err)
        # Match by subcommand
        subcmd = command[3] if len(command) > 3 and command[1] == "-C" else command[1] if len(command) > 1 else ""
        if subcmd in responses:
            res = responses[subcmd]
            if isinstance(res, tuple):
                code, out, err = res
            else:
                code, out, err = 0, res, ""
            return subprocess.CompletedProcess(args=list(command), returncode=code, stdout=out, stderr=err)
        return subprocess.CompletedProcess(args=list(command), returncode=0, stdout="", stderr="")

    return run_git


def test_dod_commit_validation_fails_when_head_remained_unchanged():
    run_git = fake_git({"rev-parse": "4f9c001"})
    res = validate_dod_item(
        0,
        "Git commit po Hermes integraci",
        repo_path="/fake/repo",
        initial_head="4f9c001",
        evidence="proveden commit",
        run_git=run_git,
    )
    assert res.valid is False
    assert any("HEAD zůstal 4f9c001" in r for r in res.reasons)


def test_dod_commit_validation_passes_when_head_changed():
    run_git = fake_git({"rev-parse": "9a8b7c6"})
    res = validate_dod_item(
        0,
        "Git commit po Hermes integraci",
        repo_path="/fake/repo",
        initial_head="4f9c001",
        evidence="vytvořen nový commit 9a8b7c6",
        run_git=run_git,
    )
    assert res.valid is True


def test_dod_cleanup_validation_fails_when_working_tree_is_dirty():
    run_git = fake_git({
        "status": "M file1.py\n?? untracked.txt",
        "diff": "",
    })
    res = validate_dod_item(
        0,
        "AO: cleanup před dalším během",
        repo_path="/fake/repo",
        evidence="cleanup proveden",
        run_git=run_git,
    )
    assert res.valid is False
    assert any("dirty" in r for r in res.reasons)


def test_dod_cleanup_validation_passes_when_clean():
    run_git = fake_git({
        "status": "",
        "diff": "",
    })
    res = validate_dod_item(
        0,
        "AO: cleanup",
        repo_path="/fake/repo",
        evidence="pracovní strom je čistý",
        run_git=run_git,
    )
    assert res.valid is True


def test_dod_remote_validation_fails_when_remote_is_empty():
    run_git = fake_git({"remote": ""})
    res = validate_dod_item(
        0,
        "záloha na remote",
        repo_path="/fake/repo",
        evidence="záloha hotova",
        run_git=run_git,
    )
    assert res.valid is False
    assert any("git remote -v je prázdný" in r for r in res.reasons)


def test_dod_remote_validation_passes_when_remote_exists_and_evidence_confirms():
    run_git = fake_git({"remote": "origin https://github.com/org/repo.git (fetch)\norigin https://github.com/org/repo.git (push)"})
    res = validate_dod_item(
        0,
        "záloha na remote",
        repo_path="/fake/repo",
        evidence="push to origin/main successful (Everything up-to-date)",
        run_git=run_git,
    )
    assert res.valid is True


def test_dod_fail_closed_on_empty_or_trivial_evidence():
    res_empty = validate_dod_item(0, "implementovat feature", evidence="")
    assert res_empty.valid is False
    assert any("chybí konkrétní ověřitelný důkaz" in r for r in res_empty.reasons)

    res_trivial = validate_dod_item(0, "implementovat feature", evidence="hotovo")
    assert res_trivial.valid is False
    assert any("obecné tvrzení 'hotovo' není konkrétním ověřitelným důkazem" in r for r in res_trivial.reasons)


def test_reproduced_p5_combined_dod_rejection():
    # Exact reproduction of P5 failure:
    # DoD: "AO: cleanup, commit a záloha po Hermes integraci"
    # Repo dirty, HEAD 4f9c001 (unchanged), remote empty
    run_git = fake_git({
        "rev-parse": "4f9c001",
        "status": "M ai_orchestrator/hermes.py\n?? scratch.py",
        "diff": "diff --git a/ai_orchestrator/hermes.py",
        "remote": "",
    })
    project = ProjectRecord(
        name="P5 — AO: cleanup, commit a záloha po Hermes integraci",
        dod=[DoDItem(text="AO: cleanup, commit a záloha po Hermes integraci", checked=False)],
    )

    report = validate_project_dod(
        project,
        repo_path="/fake/ai-orchestrator",
        initial_head="4f9c001",
        evidence="Hermes cleanup a záloha hotova",
        run_git=run_git,
    )

    assert report.is_valid is False
    assert report.rejected_indices == [0]
    assert report.verified_indices == []
    # Contains all 3 failure causes
    item_res = report.items[0]
    reasons_text = " ".join(item_res.reasons)
    assert "dirty" in reasons_text
    assert "HEAD zůstal 4f9c001" in reasons_text
    assert "git remote -v je prázdný" in reasons_text
