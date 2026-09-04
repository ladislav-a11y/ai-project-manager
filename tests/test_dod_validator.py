import subprocess
import pytest

from ai_project_manager.dod_validator import (
    get_git_diff,
    get_git_diff_check,
    get_git_head,
    get_git_remote_heads,
    get_git_branch,
    get_git_remote_branch_head,
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
        "Git commit po integraci providera",
        repo_path="/fake/repo",
        initial_head="4f9c001",
        evidence="proveden commit",
        run_git=run_git,
    )
    assert res.valid is False
    assert any("HEAD zůstal 4f9c001" in r for r in res.reasons)


def test_post_completion_policy_does_not_require_dirty_checkout_commit():
    run_git = fake_git({
        "rev-parse": "4f9c001",
        "status": " M source.py",
        "diff": "diff --git a/source.py b/source.py",
    })
    res = validate_dod_item(
        0,
        "ai-orchestrator audit ověří existující stav; nový commit není podmínkou",
        repo_path="/fake/repo",
        initial_head="4f9c001",
        evidence="628 passed; ai-orchestrator audit accepted",
        run_git=run_git,
        expected_new_commit=False,
        allow_dirty_checkout=True,
    )
    assert res.valid is True


def test_czech_independent_live_audit_does_not_require_new_commit():
    run_git = fake_git({"rev-parse": "4f9c001"})
    res = validate_dod_item(
        0,
        "Nezávislý audit ai-orchestratoru ověří relevantní chování v živém prostředí; "
        "nový commit není pro tento auditní bod vyžadován.",
        repo_path="/fake/repo",
        initial_head="4f9c001",
        evidence="1 passed; live evidence recorded; audit accepted",
        run_git=run_git,
        expected_new_commit=True,
    )
    assert res.valid is True


def test_dod_commit_validation_passes_when_head_changed():
    run_git = fake_git({"rev-parse": "9a8b7c6"})
    res = validate_dod_item(
        0,
        "Git commit po integraci providera",
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
    # DoD: "AO: cleanup, commit a záloha po integraci providera"
    # Repo dirty, HEAD 4f9c001 (unchanged), remote empty
    run_git = fake_git({
        "rev-parse": "4f9c001",
        "status": "M ai_orchestrator/provider_adapter.py\n?? scratch.py",
        "diff": "diff --git a/ai_orchestrator/provider_adapter.py",
        "remote": "",
    })
    project = ProjectRecord(
        name="P5 — AO: cleanup, commit a záloha po integraci providera",
        dod=[DoDItem(text="AO: cleanup, commit a záloha po integraci providera", checked=False)],
    )

    report = validate_project_dod(
        project,
        repo_path="/fake/ai-orchestrator",
        initial_head="4f9c001",
        evidence="cleanup a záloha hotova",
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


def test_ambiguous_validator_dod_passes_with_test_evidence_even_when_head_unchanged():
    # Exact reproduction of the P5 ambiguity:
    # DoD describes validator behavior, not a runtime action to create a commit in the audited repo.
    exact_text = "Git commit DoD ověřit proti skutečnému HEAD; požadavek na nový commit nesmí projít, pokud HEAD nezměnil očekávaný stav"
    head = "17e632beec1bf635718b67feb8b9cb28f1e742b6"
    run_git = fake_git({"rev-parse": head})

    res = validate_dod_item(
        2,
        exact_text,
        repo_path="/fake/ai-project-manager",
        initial_head=head,
        evidence="582 passed in 10.5s",
        run_git=run_git,
    )
    assert res.valid is True
    assert res.reasons == []


def test_ambiguous_validator_dod_fails_closed_without_test_evidence():
    exact_text = "Git commit DoD ověřit proti skutečnému HEAD; požadavek na nový commit nesmí projít, pokud HEAD nezměnil očekávaný stav"
    head = "17e632beec1bf635718b67feb8b9cb28f1e742b6"
    run_git = fake_git({"rev-parse": head})

    # Empty evidence fails
    res_empty = validate_dod_item(
        2,
        exact_text,
        repo_path="/fake/ai-project-manager",
        initial_head=head,
        evidence="",
        run_git=run_git,
    )
    assert res_empty.valid is False
    assert any("chybí výstup testů" in r for r in res_empty.reasons)

    # Failed test output fails
    res_failed = validate_dod_item(
        2,
        exact_text,
        repo_path="/fake/ai-project-manager",
        initial_head=head,
        evidence="580 passed, 2 failed in 8.1s",
        run_git=run_git,
    )
    assert res_failed.valid is False
    assert any("selhání" in r for r in res_failed.reasons)

    # Generic trivial placeholder fails
    res_trivial = validate_dod_item(
        2,
        exact_text,
        repo_path="/fake/ai-project-manager",
        initial_head=head,
        evidence="hotovo",
        run_git=run_git,
    )
    assert res_trivial.valid is False


def test_validation_rule_dod_proven_by_test_not_head_change():
    text1 = "ověřit, že nový commit bez změny HEAD neprojde"
    text2 = "validační DoD typu „ověřit, že nový commit bez změny HEAD neprojde“ musí být dokazatelný regresním testem, ne změnou HEAD aktuálního repa"
    head = "17e632beec1bf635718b67feb8b9cb28f1e742b6"
    run_git = fake_git({"rev-parse": head})

    for text in (text1, text2):
        res = validate_dod_item(
            0,
            text,
            repo_path="/fake/ai-project-manager",
            initial_head=head,
            evidence="pytest passed: 10 passed, 0 failed",
            run_git=run_git,
        )
        assert res.valid is True, f"Failed for text: {text}, reasons: {res.reasons}"
        assert res.reasons == []


def test_genuine_new_commit_dod_fails_when_head_unchanged():
    # A genuine DoD requesting a commit must still fail if HEAD did not change
    head = "17e632beec1bf635718b67feb8b9cb28f1e742b6"
    run_git = fake_git({"rev-parse": head})

    res = validate_dod_item(
        0,
        "vytvořit nový commit",
        repo_path="/fake/repo",
        initial_head=head,
        evidence="pytest passed: 582 passed",
        run_git=run_git,
    )
    assert res.valid is False
    assert any(f"požadavek na nový commit nesplněn: HEAD zůstal {head}" in r for r in res.reasons)


def test_genuine_new_commit_dod_passes_when_head_changed():
    initial_head = "17e632beec1bf635718b67feb8b9cb28f1e742b6"
    new_head = "9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b"
    run_git = fake_git({"rev-parse": new_head})

    res = validate_dod_item(
        0,
        "vytvořit nový commit",
        repo_path="/fake/repo",
        initial_head=initial_head,
        evidence="nový commit 9a8b7c6d vytvořen, testy prošly: 582 passed",
        run_git=run_git,
    )
    assert res.valid is True
    assert res.reasons == []


def test_existing_controller_commit_evidence_does_not_require_second_commit():
    head = "9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b"
    run_git = fake_git({"rev-parse": head, "status": "", "diff": "", "remote": "origin"})
    project = ProjectRecord(
        name="P5 — APM finalizace",
        dod=[DoDItem(text="vytvořit jeden konzistentní orchestrátorem schválený commit")],
    )

    report = validate_project_dod(
        project,
        repo_path="/fake/repo",
        initial_head=head,
        evidence='{"status":"completed","commit_hash":"%s","pushed":true,"tests_passed":true}' % head,
        run_git=run_git,
        expected_new_commit=False,
    )

    assert report.is_valid is True


def test_existing_state_audit_checks_git_without_requiring_new_commit():
    head = "9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b"
    run_git = fake_git({"rev-parse": head, "status": "M file.py", "diff": "diff", "remote": "origin", "ls-remote": f"{head} refs/heads/main", "branch": "main"})
    res = validate_dod_item(
        0,
        "accepted / rejected: ai-orchestrator audit musí ověřit HEAD/status/diff/remote/push proti realitě; nový commit není podmínkou",
        repo_path="/fake/repo",
        initial_head=head,
        evidence="0:OK read-only Git evidence ověřena; nový commit není podmínkou; 607 passed",
        run_git=run_git,
        expected_new_commit=True,
    )
    assert res.valid is True, res.reasons
    assert res.details["git_head"] == head
    assert res.details["git_status"] == "M file.py"
    assert res.details["git_remote_branch_head_ok"] is True


def test_full_p5_audit_passes_with_regression_test_evidence():
    # Card with full P5 checklist from current prompt:
    dod_texts = [
        "dohledat pravidlo/heuristiku, která z textu DoD odvozuje typ povinné evidence",
        "rozlišit DoD o implementaci validační logiky od DoD vyžadujícího konkrétní runtime Git akci v aktuálním repu",
        "Git commit DoD ověřit proti skutečnému HEAD; požadavek na nový commit nesmí projít, pokud HEAD nezměnil očekávaný stav",
        "validační DoD typu „ověřit, že nový commit bez změny HEAD neprojde“ musí být dokazatelný regresním testem, ne změnou HEAD aktuálního repa",
        "přidat regresní test pro přesně tento dvojznačný text a potvrdit správné rozlišení",
        "přidat negativní regresní test, že skutečný požadavek na nový commit bez změny HEAD stále selže",
        "žádný neověřený DoD nesmí vést k lifecycle_status=done",
        "syntaxe -> cílené testy -> git --no-pager diff -> git --no-pager diff --check -> kompletní testy podle D:\\orchestrator\\AI_PROJECT_PROTOCOL.md",
    ]
    head = "17e632beec1bf635718b67feb8b9cb28f1e742b6"
    run_git = fake_git({"rev-parse": head})

    project = ProjectRecord(
        name="P5 — APM: fail-closed validace důkazů DoD",
        dod=[DoDItem(text=t) for t in dod_texts],
    )

    report = validate_project_dod(
        project,
        repo_path="/fake/ai-project-manager",
        initial_head=head,
        evidence="pytest -q: 590 passed, 0 failed in 12.3s",
        run_git=run_git,
    )

    assert report.is_valid is True
    assert report.rejected_indices == []
    assert report.verified_indices == list(range(len(dod_texts)))
