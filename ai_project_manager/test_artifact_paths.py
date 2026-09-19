"""Shared resolution for the external, repo-external test artifact root.

Pytest's ``--basetemp`` (see ``tests/conftest.py``) and the scheduler's own
periodic cleanup (see ``artifact_cleanup.py``) must never create or target a
directory inside this checkout - a ``.pytest-basetemp-*`` directory left in
the repo tree is exactly the kind of disposable, tool-owned artifact
WORKFLOW.md's "Dočasné testovací artefakty" fail-closed lifecycle forbids.
Both call sites resolve the same external root through this module so they
can never silently drift apart.

``AI_PM_TEST_ARTIFACT_ROOT`` is the explicit override. Unset, this resolves
to a stable per-user, per-app external root: ``%LOCALAPPDATA%\\AIProjectManager\\pytest``
on Windows (``LOCALAPPDATA`` is set for every interactive and scheduled-task
Windows session), or ``~/.cache/ai-project-manager/pytest`` where
``LOCALAPPDATA`` is not set (non-Windows).
"""

from __future__ import annotations

import os
import tempfile
from uuid import uuid4
from pathlib import Path
from typing import Mapping, Optional

# Kept identical to artifact_cleanup.PYTEST_ARTIFACT_PREFIX; duplicated here
# (not imported) so this module has no dependency in either direction and
# stays usable from conftest.py before the package's own import machinery
# is guaranteed to be ready.
PYTEST_ARTIFACT_PREFIX = ".pytest-basetemp-"


class RepoLocalArtifactRootError(ValueError):
    """Raised when a test artifact root/path would land inside a checkout."""


def default_test_artifact_root(env: Optional[Mapping[str, str]] = None) -> Path:
    """Return the external root pytest basetemp/cleanup must use.

    Never returns a path inside any repository checkout - it is either the
    explicit operator override or a fixed per-user application-data
    location, neither of which is ever a git working tree.
    """
    env = os.environ if env is None else env
    override = env.get("AI_PM_TEST_ARTIFACT_ROOT")
    if override and override.strip():
        return Path(override.strip()).expanduser()
    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data and local_app_data.strip():
        return Path(local_app_data.strip()) / "AIProjectManager" / "pytest"
    return Path.home() / ".cache" / "ai-project-manager" / "pytest"


def _candidate_test_artifact_roots(env: Mapping[str, str]) -> list[Path]:
    """Return ordered external candidates for an implicit test root."""
    candidates: list[Path] = []
    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data and local_app_data.strip():
        candidates.append(Path(local_app_data.strip()) / "AIProjectManager" / "pytest")

    temp_root = env.get("TEMP") or env.get("TMP") or tempfile.gettempdir()
    candidates.append(Path(temp_root) / "AIProjectManager" / "pytest")

    home = Path.home()
    candidates.append(home / ".cache" / "ai-project-manager" / "pytest")

    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def resolve_test_artifact_root(
    env: Optional[Mapping[str, str]] = None,
    *,
    checkout_root: Optional[Path] = None,
) -> Path:
    """Return a writable external root, failing closed for bad overrides.

    An explicit override is operator-owned and therefore fails clearly when
    it is inside the checkout or cannot be created. Implicit candidates may
    fall back from a restricted LOCALAPPDATA to the user's TEMP directory.
    """
    env = os.environ if env is None else env
    override = env.get("AI_PM_TEST_ARTIFACT_ROOT")
    candidates = (
        [Path(override.strip()).expanduser()]
        if override and override.strip()
        else _candidate_test_artifact_roots(env)
    )
    errors: list[str] = []
    for candidate in candidates:
        if checkout_root is not None:
            ensure_not_repo_local(candidate, checkout_root, source="test artifact root")
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / f".write-probe-{uuid4().hex}"
            probe.write_text("probe", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
            if override and override.strip():
                break
    raise OSError(
        "No writable external pytest artifact root is available. "
        "Set AI_PM_TEST_ARTIFACT_ROOT to a writable directory outside the checkout. "
        + " | ".join(errors)
    )


def is_inside_checkout(path: Path, checkout_root: Path) -> bool:
    """True when ``path`` is (or is inside) ``checkout_root``."""
    try:
        resolved_path = path.resolve()
    except OSError:
        resolved_path = path
    try:
        resolved_root = checkout_root.resolve()
    except OSError:
        resolved_root = checkout_root
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def ensure_not_repo_local(path: Path, checkout_root: Path, *, source: str) -> Path:
    """Fail closed: refuse a path that lies inside ``checkout_root``.

    ``source`` names the caller-facing origin of ``path`` (for example
    ``"--basetemp"``) so the raised message tells an operator exactly what
    to change.
    """
    if is_inside_checkout(path, checkout_root):
        raise RepoLocalArtifactRootError(
            f"{source} {path} lies inside the repository checkout {checkout_root} - "
            "pytest basetemp must never be created inside the repo (see WORKFLOW.md's "
            "\"Dočasné testovací artefakty\" rule). Set AI_PM_TEST_ARTIFACT_ROOT to an "
            "external, writable directory outside any checkout instead."
        )
    return path
