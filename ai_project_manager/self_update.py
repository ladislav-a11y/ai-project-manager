"""Self-update detection and safe-restart preparation for the long-running
AI Project Manager process.

The PM package is imported once at process start.  When ``ai-orchestrator``
(or a human) edits this package's own source files while a long-running PM
loop is still executing, Python keeps running the *already-imported* old
module objects until the process itself exits and a new interpreter starts -
editing files on disk does not retroactively change what a running process
has already imported.  ``ai-orchestrator`` itself does not have this problem
because it is invoked as a brand-new subprocess for every dispatch and so
always re-imports current code; the long-running PM loop is the one
persistent process that can silently keep serving stale imports.

This module only *detects* the condition and *prepares* for a safe restart
(tests, a verified Git checkpoint, persisted state). It deliberately never
restarts the process itself - see ``watchdog.py`` for the separate
supervising process that performs the actual restart/rollback, per the
requirement that self-update activation must never be performed by the
agent/process whose own code is changing.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

logger = logging.getLogger("ai_project_manager")

# A dedicated, unambiguous exit code so a supervising watchdog can tell
# "restart me, on purpose, code changed" apart from a normal clean exit (0),
# a configuration error (2, see cli.py) and an ordinary operational failure
# (1). 75 is EX_TEMPFAIL in BSD sysexits.h - "temporary failure, retry" -
# which is exactly the right semantics here and avoids colliding with the
# codes cli.py already uses.
RESTART_REQUIRED_EXIT_CODE = 75

_DEFAULT_TEST_COMMAND = (sys.executable, "-m", "pytest", "-q")


def package_root() -> Path:
    """The directory whose ``*.py`` contents define "this process's code"."""
    return Path(__file__).resolve().parent


def compute_code_version(root: Optional[Path] = None) -> str:
    """A content fingerprint of every ``*.py`` file under ``root``.

    Hashing file *content* (not mtime) means a checkout operation that
    resets timestamps without changing text never produces a false
    positive, while an actual edit - committed or not, since an
    autonomous coding run may not have committed yet - is always caught.
    Deterministic and order-independent (paths are sorted) so the same
    tree always yields the same version string regardless of filesystem
    iteration order.
    """
    root = Path(root) if root is not None else package_root()
    hasher = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            content = path.read_bytes()
        except OSError as exc:
            # A file can vanish between the rglob listing and the read (a
            # concurrent edit/rename by the same self-update this is meant
            # to detect). Treat it as part of the fingerprint via its
            # absence rather than crashing the detection check.
            logger.warning("skipping unreadable file while computing code version: %s (%s)", path, exc)
            continue
        hasher.update(path.relative_to(root).as_posix().encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(content)
        hasher.update(b"\0")
    return hasher.hexdigest()


@dataclass
class SelfUpdateStatus:
    changed: bool
    started_version: str
    current_version: str


def check_self_update(started_version: str, root: Optional[Path] = None) -> SelfUpdateStatus:
    """Compare the version captured at process start against right now."""
    current_version = compute_code_version(root)
    return SelfUpdateStatus(
        changed=current_version != started_version,
        started_version=started_version,
        current_version=current_version,
    )


RunCommand = Callable[[Sequence[str]], subprocess.CompletedProcess]


def default_run_command(argv: Sequence[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@dataclass
class RestartReadiness:
    """Whether it is safe to hand off to a supervising restart right now.

    ``safe=False`` is the fail-safe path: the current (old but known-good)
    process just keeps running instead of exiting into a broken restart -
    no task is ever abandoned mid-flight because a self-update turned out
    to be untested or the working tree is in a bad state.
    """

    safe: bool
    reason: str
    tests_passed: Optional[bool] = None
    git_checkpoint_ok: Optional[bool] = None
    details: dict = field(default_factory=dict)


_CONFLICT_MARKER_PREFIXES = ("UU ", "AA ", "DD ", "AU ", "UA ", "DU ", "UD ")


def verify_git_checkpoint(
    repo_root: str,
    run_git: RunCommand = default_run_command,
) -> tuple[bool, str, dict]:
    """Confirm the working tree is a real, resolvable commit with no
    unresolved merge/rebase conflicts - i.e. a state a restart (or a
    rollback) can safely check out or reason about.

    Deliberately does not require a *clean* tree: production code changes
    arrive as uncommitted edits from an autonomous run (this project's own
    convention is that only the orchestrator commits, after tests pass),
    so demanding "no local changes" would make every legitimate self-update
    restart impossible.
    """
    head = run_git(("git", "-C", repo_root, "rev-parse", "HEAD"))
    if head.returncode != 0:
        return False, f"git HEAD is not resolvable: {head.stderr.strip() or head.stdout.strip()}", {}
    commit = head.stdout.strip()
    if not commit:
        return False, "git HEAD resolved to an empty commit id", {}

    status = run_git(("git", "-C", repo_root, "status", "--porcelain"))
    if status.returncode != 0:
        return False, f"git status failed: {status.stderr.strip() or status.stdout.strip()}", {"commit": commit}

    conflicted = [
        line for line in status.stdout.splitlines()
        if line[:3] in _CONFLICT_MARKER_PREFIXES
    ]
    if conflicted:
        return (
            False,
            f"git working tree has unresolved conflicts: {', '.join(conflicted)}",
            {"commit": commit},
        )

    return True, f"HEAD={commit}", {"commit": commit}


def run_regression_tests(
    repo_root: str,
    command: Sequence[str] = _DEFAULT_TEST_COMMAND,
    run: Callable[[Sequence[str], Optional[str]], subprocess.CompletedProcess] = default_run_command,
) -> tuple[bool, str]:
    result = run(command, repo_root)
    if result.returncode == 0:
        return True, "tests passed"
    tail = "\n".join((result.stdout or "").splitlines()[-20:])
    return False, f"tests failed (exit={result.returncode}):\n{tail}"


def prepare_safe_restart(
    repo_root: str,
    persist_state: Callable[[], None],
    test_command: Sequence[str] = _DEFAULT_TEST_COMMAND,
    run_tests: Callable[[Sequence[str], Optional[str]], subprocess.CompletedProcess] = default_run_command,
    run_git: RunCommand = default_run_command,
) -> RestartReadiness:
    """Everything that must be true *before* signalling a restart:
    regression tests pass against the current working tree, the working
    tree is a resolvable, non-conflicted Git checkpoint, and persistent
    state (provider/Trello checkpoint) has been flushed to disk.

    Order matters: state is only ever persisted (last step) once both
    checks already passed, so a failed check never leaves behind a
    freshly-written state file that a human/watchdog might mistake for a
    "ready to restart" signal.
    """
    tests_ok, tests_reason = run_regression_tests(repo_root, command=test_command, run=run_tests)
    if not tests_ok:
        logger.warning("self-update restart deferred: %s", tests_reason)
        return RestartReadiness(safe=False, reason=tests_reason, tests_passed=False)

    git_ok, git_reason, details = verify_git_checkpoint(repo_root, run_git=run_git)
    if not git_ok:
        logger.warning("self-update restart deferred: %s", git_reason)
        return RestartReadiness(
            safe=False, reason=git_reason, tests_passed=True, git_checkpoint_ok=False, details=details
        )

    try:
        persist_state()
    except Exception as exc:
        # Persistence is the final restart gate: if the provider/Trello
        # checkpoint cannot be flushed, exiting would abandon resumable
        # work and hand the watchdog incomplete state. Keep the already
        # running, known-good process alive and retry on a later tick.
        reason = f"persistent state checkpoint failed: {exc}"
        logger.exception("self-update restart deferred: %s", reason)
        return RestartReadiness(
            safe=False,
            reason=reason,
            tests_passed=True,
            git_checkpoint_ok=True,
            details=details,
        )

    return RestartReadiness(
        safe=True,
        reason="tests passed and git checkpoint verified",
        tests_passed=True,
        git_checkpoint_ok=True,
        details=details,
    )
