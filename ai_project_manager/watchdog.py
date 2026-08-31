"""Supervising watchdog for the long-running AI Project Manager process.

``ai_project_manager.cli.main`` detects a self-update, verifies it is safe
(tests + Git checkpoint - see ``self_update.py``) and exits with
``RESTART_REQUIRED_EXIT_CODE``. It never restarts itself: an already-running
Python process keeps the modules it originally imported no matter what a
child ``exec``/``spawn`` does, so only a *separate* parent process that
starts a brand-new interpreter actually picks up new code. This module is
that parent process.

Responsibilities:
  - launch the PM child process and wait for it to exit;
  - on a normal exit, stop (mirrors running the child directly);
  - on ``RESTART_REQUIRED_EXIT_CODE``, relaunch, but first run a live
    ``--once`` smoke test of the new code;
  - if the smoke test fails, run the last known-good commit's code instead
    from a dedicated, non-destructive ``git worktree`` (never
    ``git checkout --force``/``git reset --hard`` against the primary
    working tree - those would silently discard the bad, usually
    uncommitted, self-update edit with no way to recover or inspect it
    afterwards), so a bad self-update can never strand the unattended loop
    on broken code (fail-safe) *and* never destroys the evidence of what
    went wrong;
  - cap consecutive restarts so a self-update that keeps re-triggering
    itself (e.g. a flaky test) cannot spin forever.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from .self_update import RESTART_REQUIRED_EXIT_CODE, default_run_command
from .slack_notify import notify

logger = logging.getLogger("ai_project_manager.watchdog")

DEFAULT_STATE_PATH = "runtime/watchdog_state.json"
DEFAULT_LOCK_PATH = "runtime/watchdog.lock"
DEFAULT_MAX_CONSECUTIVE_RESTARTS = 5
DEFAULT_RESTART_BACKOFF_SECONDS = 5.0
ROLLBACK_WORKTREE_RELATIVE_PATH = str(Path("runtime") / "self_update_rollback_worktree")

LaunchFn = Callable[..., "subprocess.CompletedProcess"]
NotifyFn = Callable[[str], None]


class WatchdogAlreadyRunning(RuntimeError):
    """Raised when another watchdog owns the repository process lock."""


class WatchdogProcessLock:
    """Small cross-platform advisory lock held for the watchdog lifetime.

    The file is intentionally not deleted on release: ownership is attached
    to the open file handle, so the operating system releases it even after a
    crash, while deleting/recreating the pathname could let two processes lock
    different inodes at once on POSIX.
    """

    def __init__(self, path: Path):
        self.path = path
        self._handle = None
        self._mutex_handle = None

    def __enter__(self) -> "WatchdogProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                # ``msvcrt.locking`` is not reliable for this service on the
                # current Windows/Python combination: two independently
                # spawned watchdogs can both acquire the same byte range.
                # A named kernel mutex gives the scheduler a real
                # cross-process singleton while the marker file remains for
                # diagnostics and POSIX keeps using flock below.
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CreateMutexW.argtypes = [
                    wintypes.LPVOID,
                    wintypes.BOOL,
                    wintypes.LPCWSTR,
                ]
                kernel32.CreateMutexW.restype = wintypes.HANDLE
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL

                mutex_name = (
                    "Local\\AIProjectManagerWatchdog-"
                    + hashlib.sha256(str(self.path.resolve()).lower().encode("utf-8")).hexdigest()
                )
                ctypes.set_last_error(0)
                mutex = kernel32.CreateMutexW(None, True, mutex_name)
                if not mutex:
                    raise ctypes.WinError(ctypes.get_last_error())
                if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                    kernel32.CloseHandle(mutex)
                    raise WatchdogAlreadyRunning(
                        f"another AI Project Manager watchdog already owns {self.path}"
                    )
                self._mutex_handle = (kernel32, mutex)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise WatchdogAlreadyRunning(
                f"another AI Project Manager watchdog already owns {self.path}"
            ) from exc
        self._handle = handle
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt" and self._mutex_handle is not None:
                import ctypes
                from ctypes import wintypes

                kernel32, mutex = self._mutex_handle
                kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
                kernel32.ReleaseMutex.restype = wintypes.BOOL
                kernel32.ReleaseMutex(mutex)
                kernel32.CloseHandle(mutex)
                self._mutex_handle = None
            else:
                self._handle.seek(0)
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _default_launch(argv: Sequence[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), cwd=cwd, check=False)


@dataclass
class WatchdogState:
    """Persisted across watchdog restarts so a rollback decision made after
    an interpreter-level crash (not just a clean ``RESTART_REQUIRED_EXIT_CODE``
    exit) still has a last-known-good commit to fall back to."""

    last_known_good_commit: Optional[str] = None

    @classmethod
    def load(cls, path: Path) -> "WatchdogState":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("watchdog state file %s is unreadable/corrupt, starting fresh", path)
            return cls()
        if not isinstance(data, dict):
            return cls()
        commit = data.get("last_known_good_commit")
        return cls(last_known_good_commit=commit if isinstance(commit, str) else None)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        try:
            tmp.write_text(
                json.dumps({"last_known_good_commit": self.last_known_good_commit}),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        finally:
            # A locked destination or interrupted replacement must not leave
            # a shared temp file that blocks a later watchdog start.  The
            # unique name also makes concurrent watchdog starts independent.
            tmp.unlink(missing_ok=True)


def _git_head(repo_root: str, run_git: LaunchFn) -> Optional[str]:
    result = run_git(("git", "-C", repo_root, "rev-parse", "HEAD"))
    if result.returncode != 0:
        return None
    commit = (result.stdout or "").strip()
    return commit or None


def _rollback_worktree_path(repo_root: str) -> Path:
    return Path(repo_root) / ROLLBACK_WORKTREE_RELATIVE_PATH


def _resolve_python_executable(python_exe: str, repo_root: str) -> str:
    """Keep the interpreter stable when the supervised cwd changes.

    The Windows launcher intentionally accepts a repository-relative venv
    path (``.venv\\Scripts\\python.exe``).  That works for the primary child,
    but a rollback child runs with its cwd set to the isolated worktree,
    which deliberately has no venv.  Resolve path-like interpreter values
    against the primary checkout once, before constructing either command.
    Bare commands such as ``python``/``py`` remain untouched so normal PATH
    lookup still works.
    """
    path = Path(python_exe)
    if path.is_absolute():
        return str(path)
    if "/" in python_exe or "\\" in python_exe:
        return str((Path(repo_root) / path).resolve())
    return python_exe


def _prepare_rollback_worktree(repo_root: str, commit: str, run_git: LaunchFn) -> Optional[str]:
    """Non-destructively make the last known-good commit's code available to
    relaunch, via a dedicated ``git worktree`` instead of overwriting the
    primary working tree.

    A bad self-update is normally an *uncommitted* edit (this project's own
    convention is that only the orchestrator commits, after tests pass), so
    ``git checkout --force``/``git reset --hard`` against ``repo_root``
    would silently and irrecoverably discard it. ``git worktree add`` never
    writes to ``repo_root`` at all: the bad edit stays exactly where it is -
    inspectable, recoverable, still there for a human or the next autonomous
    run to fix - while the watchdog keeps the service alive by launching the
    child from this separate, known-good directory instead.
    """
    path = _rollback_worktree_path(repo_root)
    # The rollback child writes its own provider/checkpoint/runtime files, so
    # a previously used fallback worktree is normally dirty. A plain
    # ``worktree remove`` refuses to remove it and the following ``add`` then
    # fails because the directory still exists. ``--force`` is safe here:
    # the target is the fixed, application-owned rollback worktree (never the
    # primary checkout), and the primary tree containing the failed update is
    # deliberately left untouched for inspection and recovery.
    run_git(("git", "-C", repo_root, "worktree", "remove", "--force", str(path)))
    result = run_git(("git", "-C", repo_root, "worktree", "add", "--detach", str(path), commit))
    if result.returncode != 0:
        logger.error(
            "failed to prepare non-destructive rollback worktree for %s (exit=%s): %s",
            commit, result.returncode, (result.stderr or result.stdout or "").strip(),
        )
        return None
    logger.warning(
        "prepared non-destructive rollback worktree at %s for last known-good commit %s "
        "(primary working tree left untouched)", path, commit,
    )
    return str(path)


def run_watchdog(
    child_argv: Sequence[str],
    repo_root: str,
    smoke_test_argv: Optional[Sequence[str]] = None,
    state_path: Optional[str] = None,
    launch: LaunchFn = _default_launch,
    run_git: LaunchFn = default_run_command,
    max_consecutive_restarts: int = DEFAULT_MAX_CONSECUTIVE_RESTARTS,
    restart_backoff_seconds: float = DEFAULT_RESTART_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    max_cycles: Optional[int] = None,
    scheduled_task_name: str = "AI Project Manager Scheduler",
    mode: str = "persistent",
    log_path: Optional[str] = None,
    start_kind: str = "online",
    notify_fn: NotifyFn = notify,
) -> int:
    """Supervise ``child_argv`` (the PM long-running process). Returns the
    final child exit code once the child exits for a reason other than a
    verified self-update restart (or once ``max_cycles``/the restart cap is
    hit - both fail safe by returning the last exit code rather than
    looping forever)."""
    resolved_log_path = str(Path(log_path).resolve()) if log_path else str(
        (Path(repo_root) / "runtime" / "scheduler" / "scheduler.log").resolve()
    )
    # Correlation marker used by the live verifier. Keep it secret-free and
    # emit it immediately before the Slack request so an HTTP 200 receipt can
    # be attributed to this watchdog run rather than an older log entry.
    logger.info(
        "[AI Project Manager] Watchdog %s: Scheduled Task=%s; mode=%s; PID=%s; log=%s",
        start_kind,
        scheduled_task_name,
        mode,
        os.getpid(),
        resolved_log_path,
    )
    notify_fn(
        f"[AI Project Manager] Watchdog {start_kind}: "
        f"Scheduled Task={scheduled_task_name}; režim={mode}; PID={os.getpid()}; "
        f"log={resolved_log_path}"
    )

    state_file = Path(state_path or (Path(repo_root) / DEFAULT_STATE_PATH))
    state = WatchdogState.load(state_file)
    if state.last_known_good_commit is None:
        state.last_known_good_commit = _git_head(repo_root, run_git)
        state.save(state_file)

    consecutive_restarts = 0
    cycles = 0
    last_result_code = 0
    # None => launch from the primary working tree (repo_root, the normal
    # case). Set to a rollback worktree path after a failed post-restart
    # smoke test, and held there - so the service keeps running known-good
    # code - until a subsequent restart's smoke test verifies repo_root is
    # fixed, at which point it resets to None.
    active_cwd: Optional[str] = None

    while True:
        cycles += 1
        pre_launch_commit = _git_head(repo_root, run_git)

        logger.info(
            "launching AI Project Manager: %s%s",
            " ".join(child_argv),
            f" (cwd={active_cwd})" if active_cwd else "",
        )
        # Relative application paths (provider state, specs, outbox, runtime
        # files) are defined relative to the configured repository, not to
        # whichever directory happened to be current when the watchdog was
        # invoked.  ``active_cwd`` only overrides this after a rollback.
        launch_cwd = active_cwd or repo_root
        result = launch(child_argv, cwd=launch_cwd)
        last_result_code = result.returncode

        if result.returncode != RESTART_REQUIRED_EXIT_CODE:
            logger.info("child exited with code %s; watchdog stopping", result.returncode)
            if result.returncode == 0 and pre_launch_commit and active_cwd is None:
                state.last_known_good_commit = pre_launch_commit
                state.save(state_file)
            if result.returncode == 0:
                notify_fn(
                    f"[AI Project Manager] Watchdog offline/done: "
                    f"Scheduled Task={scheduled_task_name}; režim={mode}; "
                    f"PID={os.getpid()}; log={resolved_log_path}"
                )
            else:
                notify_fn(
                    f"[AI Project Manager] Watchdog spadl: child exit code "
                    f"{result.returncode}; Scheduled Task={scheduled_task_name}; "
                    f"log={resolved_log_path}"
                )
            return result.returncode

        consecutive_restarts += 1
        logger.warning(
            "child requested a self-update restart (%d/%d consecutive)",
            consecutive_restarts, max_consecutive_restarts,
        )
        if consecutive_restarts > max_consecutive_restarts:
            logger.error(
                "exceeded max consecutive self-update restarts (%d); staying down to avoid a crash loop",
                max_consecutive_restarts,
            )
            notify_fn(
                f"[AI Project Manager] Watchdog spadl: překročen limit "
                f"{max_consecutive_restarts} restartů; Scheduled Task={scheduled_task_name}; "
                f"log={resolved_log_path}"
            )
            return RESTART_REQUIRED_EXIT_CODE

        if sleep is not None and restart_backoff_seconds > 0:
            sleep(restart_backoff_seconds)

        if smoke_test_argv:
            logger.info("running post-restart smoke test: %s", " ".join(smoke_test_argv))
            # Verify the candidate in the primary checkout.  In particular,
            # never inherit a caller cwd or test the rollback worktree here:
            # the smoke test decides whether ``repo_root`` is safe to resume.
            smoke = launch(smoke_test_argv, cwd=repo_root)
            if smoke.returncode != 0:
                logger.error(
                    "post-restart smoke test failed (exit=%s); falling back to a "
                    "non-destructive rollback instead of touching repo_root",
                    smoke.returncode,
                )
                if state.last_known_good_commit:
                    rollback_dir = _prepare_rollback_worktree(
                        repo_root, state.last_known_good_commit, run_git
                    )
                    if rollback_dir:
                        active_cwd = rollback_dir
                    else:
                        # ``_prepare_rollback_worktree`` first removes any
                        # previous fallback before creating a fresh one. If
                        # creation then fails, an earlier ``active_cwd`` now
                        # points at a directory that no longer exists. Clear
                        # it so the next cycle can still launch from the
                        # primary checkout instead of crashing in
                        # ``subprocess.run(..., cwd=missing_path)``.
                        active_cwd = None
                        logger.error(
                            "could not prepare rollback worktree; primary working tree "
                            "left untouched, will retry current (untested) code next cycle"
                        )
                else:
                    logger.error(
                        "no last known-good commit recorded; cannot roll back - "
                        "primary working tree left untouched"
                    )
                # Keep the failure streak intact.  Resetting it here lets a
                # candidate that repeatedly fails its smoke test evade
                # ``max_consecutive_restarts`` forever: every fallback cycle
                # would otherwise start counting from zero again.  Only a
                # passing smoke test below proves the restart chain healthy
                # enough to reset the guard.
                if max_cycles is not None and cycles >= max_cycles:
                    notify_fn(
                        f"[AI Project Manager] Watchdog spadl: smoke test exit code "
                        f"{smoke.returncode}; Scheduled Task={scheduled_task_name}; "
                        f"log={resolved_log_path}"
                    )
                    return smoke.returncode
                continue

            logger.info("post-restart smoke test passed; new version is live")
            active_cwd = None
            current_commit = _git_head(repo_root, run_git)
            if current_commit:
                state.last_known_good_commit = current_commit
                state.save(state_file)
            # A verified-healthy restart is not a crash loop - only
            # restarts that never reach a passing smoke test should count
            # toward max_consecutive_restarts, otherwise a long-lived
            # process that legitimately self-updates many times over days
            # would eventually hit the cap and refuse to restart at all.
            consecutive_restarts = 0

        if max_cycles is not None and cycles >= max_cycles:
            notify_fn(
                f"[AI Project Manager] Watchdog offline/done: "
                f"Scheduled Task={scheduled_task_name}; režim={mode}; "
                f"PID={os.getpid()}; log={resolved_log_path}"
            )
            return last_result_code
        # loop again: relaunch the (now-current) long-running child, which
        # resumes from whatever checkpoint the previous process persisted.


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-project-manager-watchdog")
    parser.add_argument("--repo-root", default=None, help="repository root for Git checkpoint/rollback")
    parser.add_argument(
        "--python-exe", default=sys.executable, help="python interpreter used to launch the child process"
    )
    parser.add_argument(
        "--no-smoke-test", action="store_true", help="skip the post-restart --once smoke test"
    )
    parser.add_argument(
        "--max-consecutive-restarts",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_RESTARTS,
        help="maximum failed self-update restart attempts before staying down",
    )
    parser.add_argument(
        "--restart-backoff-seconds",
        type=float,
        default=DEFAULT_RESTART_BACKOFF_SECONDS,
        help="delay before testing and relaunching after a self-update request",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--scheduled-task-name", default="AI Project Manager Scheduler")
    parser.add_argument("--mode", default="persistent")
    parser.add_argument("--log-path", default=None)
    parser.add_argument("--start-kind", choices=("online", "restart"), default="online")
    parser.add_argument(
        "child_args", nargs=argparse.REMAINDER,
        help="arguments forwarded to 'python -m ai_project_manager' (e.g. --log-level DEBUG)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_consecutive_restarts < 0:
        parser.error("--max-consecutive-restarts must be non-negative")
    if not math.isfinite(args.restart_backoff_seconds) or args.restart_backoff_seconds < 0:
        parser.error("--restart-backoff-seconds must be a finite non-negative number")
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    repo_root = args.repo_root or str(Path(__file__).resolve().parent.parent)
    python_exe = _resolve_python_executable(args.python_exe, repo_root)
    forwarded = [a for a in args.child_args if a != "--"]
    child_argv = [python_exe, "-m", "ai_project_manager", *forwarded]
    smoke_test_argv = None
    if not args.no_smoke_test:
        smoke_test_argv = [python_exe, "-m", "ai_project_manager", "--once"]

    lock_path = Path(repo_root) / DEFAULT_LOCK_PATH
    try:
        with WatchdogProcessLock(lock_path):
            return run_watchdog(
                child_argv,
                repo_root=repo_root,
                smoke_test_argv=smoke_test_argv,
                max_consecutive_restarts=args.max_consecutive_restarts,
                restart_backoff_seconds=args.restart_backoff_seconds,
                scheduled_task_name=args.scheduled_task_name,
                mode=args.mode,
                log_path=args.log_path,
                start_kind=args.start_kind,
            )
    except WatchdogAlreadyRunning as exc:
        logger.error("%s; refusing to launch a duplicate scheduler", exc)
        return 1
    except KeyboardInterrupt:
        log_path = args.log_path or str(Path(repo_root) / "runtime" / "scheduler" / "scheduler.log")
        notify(
            f"[AI Project Manager] Watchdog offline/done: čisté ukončení; "
            f"Scheduled Task={args.scheduled_task_name}; režim={args.mode}; "
            f"PID={os.getpid()}; log={Path(log_path).resolve()}"
        )
        logger.info("watchdog stopped by operator")
        return 0
    except Exception as exc:
        # Keep the Slack cause intentionally short and secret-safe; the full
        # exception and traceback remain in the referenced transcript.
        log_path = args.log_path or str(Path(repo_root) / "runtime" / "scheduler" / "scheduler.log")
        notify(
            f"[AI Project Manager] Watchdog spadl: {type(exc).__name__}; "
            f"Scheduled Task={args.scheduled_task_name}; log={Path(log_path).resolve()}"
        )
        logger.exception("watchdog terminated unexpectedly")
        return 1


if __name__ == "__main__":
    sys.exit(main())
