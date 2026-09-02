"""Fail-closed cleanup for disposable, tool-owned test artifacts.

Only direct children whose names carry the pytest basetemp prefix are ever
eligible.  Callers must invoke cleanup between autonomous runs; the explicit
``active_run`` gate makes an accidental in-run call a no-op as well.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

PYTEST_ARTIFACT_PREFIX = ".pytest-basetemp-"


def cleanup_test_artifacts(
    root: str | Path,
    *,
    retention_seconds: float,
    active_run: bool,
    now: Callable[[], float] = time.time,
) -> list[Path]:
    """Remove expired pytest basetemp directories and return removed paths.

    The root itself is never removed or traversed recursively for discovery.
    Symlinks, files, non-matching names, fresh directories and anything while
    an autonomous run is active are preserved.  Individual I/O failures are
    logged and leave the artifact in place.
    """
    if active_run:
        return []
    if retention_seconds < 0:
        raise ValueError("retention_seconds must not be negative")

    cleanup_root = Path(root).resolve()
    if not cleanup_root.is_dir():
        return []

    removed: list[Path] = []
    cutoff = now() - retention_seconds
    try:
        candidates: Iterable[Path] = cleanup_root.iterdir()
        for candidate in candidates:
            if not candidate.name.startswith(PYTEST_ARTIFACT_PREFIX):
                continue
            try:
                # Never follow a link or accept a nested/escaped candidate.
                if candidate.is_symlink() or not candidate.is_dir():
                    continue
                if candidate.parent.resolve() != cleanup_root:
                    continue
                if candidate.stat().st_mtime > cutoff:
                    continue
                shutil.rmtree(candidate)
                removed.append(candidate)
            except OSError as exc:
                logger.warning("could not remove stale test artifact %s: %s", candidate, exc)
    except OSError as exc:
        logger.warning("could not inspect test artifact root %s: %s", cleanup_root, exc)
    return removed
