"""Fail-closed validation for Definition of Done (DoD) items.

Every DoD item must be substantiated by concrete, verifiable evidence before
it can be marked as checked or accepted in an audit.

Key validations:
- Git commit DoD: checked against actual HEAD commit; requires git HEAD to be resolvable
  and, if a new commit was required, to have changed from initial HEAD.
- Clean/dirty DoD: checked against actual git status and git diff; dirty tree fails validation.
- Remote DoD: checked against actual git remote -v; empty remote fails validation.
- Backup/push DoD: requires configured remote and explicit proof of push/backup.
- Test DoD: requires test execution evidence showing passed tests and no failures.
- Generic DoD: fail-closed validation requiring non-empty, concrete evidence matching the task.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

from .models import DoDItem, ProjectRecord

logger = logging.getLogger("ai_project_manager.dod_validator")

RunCommand = Callable[..., subprocess.CompletedProcess]


def default_run_command(command: Sequence[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def get_git_head(repo_path: Optional[str], run_git: RunCommand = default_run_command) -> Optional[str]:
    if not repo_path:
        return None
    try:
        res = run_git(("git", "-C", str(repo_path), "rev-parse", "HEAD"))
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception as exc:
        logger.debug("get_git_head failed on %s: %s", repo_path, exc)
    return None


def get_git_status(repo_path: Optional[str], run_git: RunCommand = default_run_command) -> tuple[bool, str]:
    if not repo_path:
        return False, "repo path not specified"
    try:
        res = run_git(("git", "-C", str(repo_path), "status", "--porcelain"))
        if res.returncode != 0:
            return False, res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}"
        return True, res.stdout.strip()
    except Exception as exc:
        return False, str(exc)


def get_git_diff(repo_path: Optional[str], run_git: RunCommand = default_run_command) -> tuple[bool, str]:
    if not repo_path:
        return False, "repo path not specified"
    try:
        res = run_git(("git", "-C", str(repo_path), "diff"))
        if res.returncode != 0:
            return False, res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}"
        return True, res.stdout.strip()
    except Exception as exc:
        return False, str(exc)


def get_git_diff_check(repo_path: Optional[str], run_git: RunCommand = default_run_command) -> tuple[bool, str]:
    """Run the exact whitespace/error check required by the runtime contract."""
    if not repo_path:
        return False, "repo path not specified"
    try:
        res = run_git(("git", "-C", str(repo_path), "diff", "--check"))
        return res.returncode == 0, res.stderr.strip() or res.stdout.strip()
    except Exception as exc:
        return False, str(exc)


def get_git_remote_heads(repo_path: Optional[str], run_git: RunCommand = default_run_command) -> tuple[bool, str]:
    """Verify that the configured origin exposes real remote branch state."""
    if not repo_path:
        return False, "repo path not specified"
    try:
        res = run_git(("git", "-C", str(repo_path), "ls-remote", "--heads", "origin"))
        output = res.stderr.strip() or res.stdout.strip()
        return res.returncode == 0 and bool(res.stdout.strip()), output
    except Exception as exc:
        return False, str(exc)


def get_git_branch(repo_path: Optional[str], run_git: RunCommand = default_run_command) -> Optional[str]:
    if not repo_path:
        return None
    try:
        res = run_git(("git", "-C", str(repo_path), "branch", "--show-current"))
        return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else None
    except Exception as exc:
        logger.debug("get_git_branch failed on %s: %s", repo_path, exc)
        return None


def get_git_remote_branch_head(
    repo_path: Optional[str], branch: Optional[str], run_git: RunCommand = default_run_command
) -> tuple[bool, str]:
    """Read the exact origin branch hash used by the current checkout."""
    if not repo_path or not branch:
        return False, "current branch not specified"
    try:
        res = run_git(("git", "-C", str(repo_path), "ls-remote", "origin", f"refs/heads/{branch}"))
        output = res.stderr.strip() or res.stdout.strip()
        return res.returncode == 0 and bool(res.stdout.strip()), output
    except Exception as exc:
        return False, str(exc)


def get_git_remotes(repo_path: Optional[str], run_git: RunCommand = default_run_command) -> tuple[bool, str]:
    if not repo_path:
        return False, "repo path not specified"
    try:
        res = run_git(("git", "-C", str(repo_path), "remote", "-v"))
        if res.returncode != 0:
            return False, res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}"
        return True, res.stdout.strip()
    except Exception as exc:
        return False, str(exc)


@dataclass
class ItemValidationResult:
    index: int
    text: str
    valid: bool
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


@dataclass
class DoDValidationReport:
    is_valid: bool
    items: list[ItemValidationResult]
    verified_indices: list[int]
    rejected_indices: list[int]
    rejection_summary: str = ""


_COMMIT_KEYWORD_RE = re.compile(
    r"(?i)\b(?:commit[a-z]*|commity|zacommitov[a-z]*|head)\b"
)
_CLEANUP_KEYWORD_RE = re.compile(
    r"(?i)\b(?:cleanup|clean|čist[ýáé]|dirty|neuložen[éý][a-z]*\s+změn[a-z]*|pracovní[a-z]*\s+strom|žádné\s+neuložené)\b"
)
_REMOTE_BACKUP_KEYWORD_RE = re.compile(
    r"(?i)\b(?:remote|záloh[a-z]*|backup|push|pushnout|odsunout)\b"
)
_TEST_KEYWORD_RE = re.compile(
    r"(?i)\b(?:test[yůeai]?|pytest|unittest|syntax[eai]?|diff\s+--check|sada|suite)\b"
)
_GENERIC_TRIVIAL_EVIDENCE_RE = re.compile(
    r"(?i)^\s*(?:done|ok|hotovo|vše\s+splněno|splněno|completed|pass|passed)\s*$"
)
_VALIDATION_META_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:validac[eai]|validačn[íý]|validátor[a-z]*|validate|validating|validation|validator)\b|"
    r"\b(?:pravidl[oa]|heuristik[a-z]*|rule|heuristics)\b|"
    r"\b(?:specifikac[eai]|specifikuj[eai]|specification|popisuj[eai]|chování\s+validátoru|auditní\s+semantik[a-z]*)\b|"
    r"\b(?:rozlišit|implementovat\s+validaci|implementace\s+validačn[íý])\b|"
    r"\b(?:regresn[íý]\s+test[a-z]*|negativn[íý]\s+test[a-z]*|unit\s*test[a-z]*|testovac[íý]\s+případ[a-z]*|dokazateln[ýé]\s+testem)\b|"
    r"\b(?:ověřit,\s*že|ověření,\s*že|ověřit\s+proti|ověřovat|ověřuje|verify\s+that|check\s+that|testovat,\s*že)\b|"
    r"\b(?:dod\s+ověřit|dod\s+popisující|dod\s+typu|validační\s+dod|skutečný\s+dod)\b|"
    r"\b(?:nesmí\s+projít|musí\s+selhat|nesmí\s+být\s+přijat|musí\s+být\s+zamítnut|must\s+fail|must\s+not\s+pass|should\s+fail|neprojde)\b|"
    r"\b(?:pokud\s+.*(?:nezměn[a-z]*|nemá|selh[a-z]*|chyb[a-z]*|beze?\s+změny)|when\s+.*(?:unchanged|fails|missing)|without\s+.*(?:change))\b|"
    r"\b(?:požadavek\s+na\s+.*(?:nesmí|musí|selh[a-z]*))\b"
    r")"
)
_ORCHESTRATOR_AUDIT_EVIDENCE_RE = re.compile(
    r"(?i)\b(?:accepted\s*/\s*rejected|ai[- ]orchestrator\s+audit|"
    r"independent\s+audit)\b"
)
_AUDIT_GIT_STATE_RE = re.compile(
    r"(?i)\b(?:HEAD|status|diff|remote|push)\b"
)
_VERIFICATION_SEQUENCE = (
    "syntax",
    "targeted tests",
    "git --no-pager diff",
    "git --no-pager diff --check",
    "complete tests",
)


def validate_dod_item(
    index: int,
    item_text: str,
    repo_path: Optional[str] = None,
    initial_head: Optional[str] = None,
    evidence: Optional[str] = None,
    run_git: RunCommand = default_run_command,
    expected_new_commit: bool = True,
) -> ItemValidationResult:
    text = (item_text or "").strip()
    reasons = []
    details = {}
    matched_category = False

    evidence_str = (evidence or "").strip()
    # An explicit orchestrator audit item describes evidence semantics; it is
    # not a request for the audited repository to create a commit/push.
    is_meta_or_validation = bool(
        _VALIDATION_META_RE.search(text)
        or _ORCHESTRATOR_AUDIT_EVIDENCE_RE.search(text)
    )

    # Audit wording suppresses the *new-commit* and direct push-action
    # heuristics, but it must not suppress the underlying read-only Git
    # checks. This keeps "verify existing state" both no-commit and
    # evidence-backed against the real checkout.
    if _ORCHESTRATOR_AUDIT_EVIDENCE_RE.search(text) and _AUDIT_GIT_STATE_RE.search(text):
        matched_category = True
        current_head = get_git_head(repo_path, run_git=run_git)
        details["git_head"] = current_head
        if not current_head:
            reasons.append("git HEAD repozitáře nelze ověřit")
        status_ok, status_out = get_git_status(repo_path, run_git=run_git)
        details["git_status_ok"] = status_ok
        details["git_status"] = status_out
        if not status_ok:
            reasons.append(f"ověření git status selhalo: {status_out}")
        diff_ok, diff_out = get_git_diff(repo_path, run_git=run_git)
        details["git_diff_ok"] = diff_ok
        if not diff_ok:
            reasons.append(f"ověření git diff selhalo: {diff_out}")
        remotes_ok, remotes_out = get_git_remotes(repo_path, run_git=run_git)
        details["git_remotes_ok"] = remotes_ok
        details["git_remotes"] = remotes_out
        if not remotes_ok:
            reasons.append(f"ověření git remote -v selhalo: {remotes_out}")
        elif not remotes_out:
            reasons.append("git remote -v je prázdný: nelze ověřit remote stav")
        diff_check_ok, diff_check_out = get_git_diff_check(repo_path, run_git=run_git)
        details["git_diff_check_ok"] = diff_check_ok
        details["git_diff_check"] = diff_check_out
        if not diff_check_ok:
            reasons.append(f"ověření git diff --check selhalo: {diff_check_out}")
        remote_heads_ok, remote_heads_out = get_git_remote_heads(repo_path, run_git=run_git)
        details["git_remote_heads_ok"] = remote_heads_ok
        details["git_remote_heads"] = remote_heads_out
        if not remote_heads_ok:
            reasons.append(f"ověření remote HEAD/branchí selhalo: {remote_heads_out}")
        branch = get_git_branch(repo_path, run_git=run_git)
        branch_ok, branch_out = get_git_remote_branch_head(repo_path, branch, run_git=run_git)
        details["git_branch"] = branch
        details["git_remote_branch_head_ok"] = branch_ok
        details["git_remote_branch_head"] = branch_out
        if not branch_ok:
            reasons.append(f"ověření konkrétní vzdálené větve selhalo: {branch_out}")
        elif current_head and not re.search(rf"(?m)^{re.escape(current_head)}\s+refs/heads/{re.escape(branch)}$", branch_out):
            reasons.append(
                f"lokální HEAD {current_head} se neshoduje s origin/{branch}: {branch_out}"
            )

    if (
        _ORCHESTRATOR_AUDIT_EVIDENCE_RE.search(text)
        and "AI_PROJECT_RUNTIME.md" in text
        and repo_path
    ):
        runtime_path = Path(repo_path).resolve().parent / "AI_PROJECT_RUNTIME.md"
        try:
            runtime_text = runtime_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            runtime_text = ""
            reasons.append(f"kanonický runtime contract nelze načíst: {exc}")
        positions = [runtime_text.casefold().find(step.casefold()) for step in _VERIFICATION_SEQUENCE]
        details["verification_sequence"] = list(_VERIFICATION_SEQUENCE)
        details["verification_sequence_positions"] = positions
        if any(position < 0 for position in positions) or positions != sorted(positions):
            reasons.append(
                "kanonický runtime contract neobsahuje ověřovací kroky v požadovaném pořadí"
            )

    # 1. Clean/dirty tree check (only for direct runtime actions on the audited repo)
    if _CLEANUP_KEYWORD_RE.search(text) and not is_meta_or_validation:
        matched_category = True
        status_ok, status_out = get_git_status(repo_path, run_git=run_git)
        details["git_status_ok"] = status_ok
        details["git_status"] = status_out
        if not status_ok:
            reasons.append(f"ověření git status selhalo: {status_out}")
        elif status_out:
            lines = status_out.splitlines()
            reasons.append(
                f"pracovní strom je dirty (nalezeno {len(lines)} neuložených změn v git status)"
            )

        diff_ok, diff_out = get_git_diff(repo_path, run_git=run_git)
        details["git_diff_ok"] = diff_ok
        if diff_ok and diff_out:
            reasons.append("pracovní strom obsahuje neuložené změny v git diff")

    # 2. Git commit check (only for direct runtime actions on the audited repo)
    if _COMMIT_KEYWORD_RE.search(text) and not is_meta_or_validation:
        matched_category = True
        current_head = get_git_head(repo_path, run_git=run_git)
        details["git_head"] = current_head
        details["initial_head"] = initial_head
        if not current_head:
            reasons.append("git HEAD repozitáře nelze zjistit nebo repozitář neexistuje")
        else:
            if initial_head and current_head == initial_head and expected_new_commit:
                reasons.append(
                    f"požadavek na nový commit nesplněn: HEAD zůstal {current_head} (žádný nový commit nevznikl)"
                )
            if evidence_str and re.search(
                r"(?i)\b(?:no\s+commit|commit\s+skipped|commit\s+failed|nevznikl\s+commit)\b", evidence_str
            ):
                reasons.append("důkaz uvádí, že commit nebyl vytvořen")

    # 3. Remote / backup / push check (only for direct runtime actions on the audited repo)
    if _REMOTE_BACKUP_KEYWORD_RE.search(text) and not is_meta_or_validation:
        matched_category = True
        remotes_ok, remotes_out = get_git_remotes(repo_path, run_git=run_git)
        details["git_remotes_ok"] = remotes_ok
        details["git_remotes"] = remotes_out
        if not remotes_ok:
            reasons.append(f"ověření git remote -v selhalo: {remotes_out}")
        elif not remotes_out:
            reasons.append(
                "git remote -v je prázdný: repozitář nemá žádný nastavený remote pro push/zálohu; absence remote brání splnění zálohy"
            )
        else:
            if not evidence_str:
                reasons.append("chybí explicitní důkaz o provedení zálohy / push na remote")
            elif re.search(
                r"(?i)\b(?:no\s+remote|remote\s+is\s+empty|prázdný\s+remote|push\s+failed|fatal:\s+no\s+configured\s+push)\b",
                evidence_str,
            ):
                reasons.append("důkaz nepotvrzuje úspěšnou zálohu/push na remote (remote chybí nebo push selhal)")

    # 4. Test check and Validation logic check
    if _TEST_KEYWORD_RE.search(text) or is_meta_or_validation:
        matched_category = True
        if not evidence_str:
            reasons.append("chybí výstup testů jako důkaz pro testovací/validační DoD bod")
        else:
            has_failed = bool(
                re.search(r"(?i)\b(?:failed|errors?|syntaxerror|failure)\b", evidence_str)
                and not re.search(r"(?i)\b(?:0\s+failed|0\s+errors|0\s+failures)\b", evidence_str)
            )
            has_passed = bool(
                re.search(r"(?i)\b(?:passed|ok|tests\s+passed|testy\s+prošly|úspěch|\d+\s+passed)\b", evidence_str)
            )
            if has_failed:
                reasons.append("výstup testů obsahuje selhání nebo chyby")
            elif not has_passed:
                reasons.append("výstup testů neobsahuje potvrzení o úspěšném proběhnutí testů")

    # 5. General fail-closed validation for other / all items
    if not evidence_str:
        if not matched_category or not reasons:
            reasons.append(f"chybí konkrétní ověřitelný důkaz pro DoD bod: '{text}'")
    elif _GENERIC_TRIVIAL_EVIDENCE_RE.match(evidence_str):
        reasons.append(
            f"obecné tvrzení '{evidence_str}' není konkrétním ověřitelným důkazem pro DoD bod: '{text}'"
        )

    is_valid = len(reasons) == 0
    return ItemValidationResult(
        index=index,
        text=text,
        valid=is_valid,
        reasons=reasons,
        details=details,
    )


def validate_project_dod(
    project_or_dod: Union[ProjectRecord, list[DoDItem], list[str]],
    repo_path: Optional[str] = None,
    initial_head: Optional[str] = None,
    evidence: Optional[str] = None,
    run_git: RunCommand = default_run_command,
    target_indices: Optional[Sequence[int]] = None,
    expected_new_commit: bool = True,
) -> DoDValidationReport:
    if isinstance(project_or_dod, ProjectRecord):
        dod_items = project_or_dod.dod
    elif isinstance(project_or_dod, list):
        dod_items = project_or_dod
    else:
        dod_items = []

    results: list[ItemValidationResult] = []
    indices_to_check = set(target_indices) if target_indices is not None else set(range(len(dod_items)))

    for idx, item in enumerate(dod_items):
        if idx not in indices_to_check:
            continue
        text = item.text if isinstance(item, DoDItem) else str(item)
        res = validate_dod_item(
            index=idx,
            item_text=text,
            repo_path=repo_path,
            initial_head=initial_head,
            evidence=evidence,
            run_git=run_git,
            expected_new_commit=expected_new_commit,
        )
        if _ORCHESTRATOR_AUDIT_EVIDENCE_RE.search(text) and evidence:
            indexed = re.search(rf"(?m)^\s*{idx}:(OK|REJECT)\b", evidence)
            if indexed is not None and indexed.group(1) != "OK":
                res.reasons.append("audit evidence for this exact DoD index is rejected")
                res.valid = False
            elif indexed is None and re.search(r"(?m)^\s*\d+:(?:OK|REJECT)\b", evidence):
                res.reasons.append("chybí per-index audit evidence pro tento DoD bod")
                res.valid = False
        results.append(res)

    verified_indices = [r.index for r in results if r.valid]
    rejected_indices = [r.index for r in results if not r.valid]
    is_valid = len(rejected_indices) == 0 and len(results) > 0

    rejection_lines = [
        f"bod [{r.index}] ({r.text}): {'; '.join(r.reasons)}"
        for r in results
        if not r.valid
    ]
    rejection_summary = "; ".join(rejection_lines)

    return DoDValidationReport(
        is_valid=is_valid,
        items=results,
        verified_indices=verified_indices,
        rejected_indices=rejected_indices,
        rejection_summary=rejection_summary,
    )
