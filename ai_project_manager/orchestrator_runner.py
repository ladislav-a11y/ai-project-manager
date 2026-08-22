"""Real ``run_fn``: hands a project to the actual ai-orchestrator process
in autonomous mode and turns its result back into the dict shape
``runner.run_once`` expects.

ai-orchestrator's real CLI (``orchestrator.py``) is an argv-driven
autonomous command - it does not read a task off stdin, and it does not
print a JSON result to stdout. A run is invoked as::

    <command...> --project <local-project-path> --goal <goal text> \
        --spec <path-to-spec-file> --agent <provider>

and its result is written as a JSON file under an outbox directory
(``outbox/autonomous-<project-slug>*.json``), which this module reads
back after the process exits.

Two extra pieces of configuration make that contract work without any
hardcoded single project or path:

- ``project_paths``/``projects_root`` map a Trello-backed
  ``ProjectRecord`` onto the local checkout ai-orchestrator should
  operate on (see ``resolve_project_path``).
- ``spec_dir`` holds one stable, per-project spec file (named after the
  project's slug, overwritten every run) carrying the task/DoD/checkpoint
  - see ``write_spec_file`` - so ai-orchestrator's own checkpoint resume
  keys off a spec whose identity never changes between runs.

Any sign of a session/quota limit - a non-zero exit whose output looks
like a limit, a raised subprocess error, or an explicit
``limit_hit``/``session_limit`` field in the outbox JSON - is routed
through ``providers.detect_limit`` and immediately recorded on the
``ProviderRegistry`` (state -> LIMITED, retry_after set) so the
scheduler will not try this provider again before then.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from typing import Callable, Optional

from .models import ProjectRecord
from .orchestrator_handoff import OrchestratorTask, build_orchestrator_task
from .providers import ProviderRegistry, detect_limit

# command (argv, already including --project/--goal/--spec/--agent) ->
# a subprocess.CompletedProcess-like object with .returncode, .stdout,
# .stderr. Injectable so tests never spawn a real process and callers can
# point at any ai-orchestrator invocation shape.
SubprocessFn = Callable[[list], "subprocess.CompletedProcess"]

# (outbox_dir, project_name, since_timestamp) -> the parsed result JSON.
# Injectable so tests can fake the outbox without touching the filesystem.
ReadOutboxFn = Callable[[str, str, float], dict]

_DEFAULT_LIMIT_BACKOFF = timedelta(minutes=30)
_DEFAULT_SPEC_DIR = "specs"
_DEFAULT_OUTBOX_DIR = "outbox"


def _default_subprocess_run(command: list) -> "subprocess.CompletedProcess":
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )


class OrchestratorProcessError(RuntimeError):
    """Raised when ai-orchestrator fails in a way that is not a
    recognizable session/quota limit."""


class ProjectPathError(RuntimeError):
    """Raised when a project cannot be mapped to a local checkout path."""


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "project"


def resolve_project_path(
    project: ProjectRecord,
    project_paths: Optional[dict] = None,
    projects_root: Optional[str] = None,
) -> str:
    """Map a Trello-backed ``ProjectRecord`` onto the local checkout
    ai-orchestrator's ``--project`` argument should point at.

    Resolution order (no single hardcoded path):
      1. an explicit per-project override in ``project_paths`` (keyed by
         ``project.name``, e.g. from ``AI_PM_PROJECT_PATHS``);
      2. ``<projects_root>/<slug(project.name)>`` when a shared root is
         configured (e.g. from ``AI_PM_PROJECTS_ROOT``).

    A project matching neither raises loudly rather than silently
    guessing a path.
    """
    project_paths = project_paths or {}
    if project.name in project_paths:
        return project_paths[project.name]
    if projects_root:
        return str(Path(projects_root) / _slugify(project.name))
    raise ProjectPathError(
        f"cannot resolve local path for project {project.name!r}: "
        "configure AI_PM_PROJECT_PATHS (per-project) or AI_PM_PROJECTS_ROOT (shared base dir)"
    )


def spec_file_path(spec_dir: str, project_name: str) -> Path:
    """Stable per-project spec path - one file per project slug,
    overwritten every run - so ai-orchestrator's checkpoint resume keys
    off a spec whose identity doesn't change between runs."""
    return Path(spec_dir) / f"{_slugify(project_name)}.json"


def write_spec_file(spec_dir: str, task: OrchestratorTask) -> Path:
    """Write the Definition of Done (plus goal/checkpoint) for this run to
    the project's stable spec file and return its path."""
    path = spec_file_path(spec_dir, task.project_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(task.to_dict(), indent=2), encoding="utf-8")
    return path


def _read_outbox_result(outbox_dir: str, project_name: str, since_ts: float) -> dict:
    slug = _slugify(project_name)
    outbox = Path(outbox_dir)
    candidates = sorted(
        outbox.glob(f"autonomous-{slug}*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"no outbox result found for project {project_name!r} in {outbox_dir!r} "
            f"(expected autonomous-{slug}*.json)"
        )
    newest = candidates[0]
    if newest.stat().st_mtime < since_ts - 1.0:
        raise FileNotFoundError(
            f"outbox result for project {project_name!r} in {outbox_dir!r} is stale "
            "(last written before this run started) - ai-orchestrator may not have produced a new result"
        )
    with newest.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _mark_limited_result(
    provider_registry: ProviderRegistry,
    provider: str,
    project: ProjectRecord,
    retry_after: timedelta,
    reason: str,
    checkpoint: Optional[dict] = None,
) -> dict:
    status = provider_registry.mark_limited(
        provider,
        retry_after=retry_after,
        checkpoint=checkpoint if checkpoint is not None else project.checkpoint,
        reason=reason,
    )
    # Deliberately NOT ProjectStatus.PAUSED: that status is permanently
    # unschedulable (see scheduler.NOT_SCHEDULABLE_STATUSES) and would
    # wedge the project forever. IN_PROGRESS keeps it schedulable so it
    # resumes automatically as soon as the provider becomes available
    # again - the ProviderRegistry, not the project status, is what
    # gates further attempts until retry_after.
    result: dict = {
        "status": "in_progress",
        "stop_reason": f"provider session limit hit: {reason}",
        "retry_after": status.retry_after.isoformat(),
    }
    if checkpoint is not None:
        result["checkpoint"] = checkpoint
    return result


def build_run_fn(
    provider_registry: ProviderRegistry,
    command: list,
    project_paths: Optional[dict] = None,
    projects_root: Optional[str] = None,
    spec_dir: str = _DEFAULT_SPEC_DIR,
    outbox_dir: str = _DEFAULT_OUTBOX_DIR,
    subprocess_run: SubprocessFn = _default_subprocess_run,
    read_outbox: ReadOutboxFn = _read_outbox_result,
    definition_of_done: Optional[list] = None,
    clock: Callable[[], float] = time.time,
):
    """Build a ``run_fn(project, provider) -> dict`` that dispatches to the
    real ai-orchestrator ``--project``/``--goal``/``--spec``/``--agent``
    autonomous CLI, carrying the project's goal/DoD/checkpoint via a
    stable per-project spec file, and reads the result back from the
    outbox instead of assuming JSON on stdout.
    """

    def run_fn(project: ProjectRecord, provider: str) -> dict:
        task = build_orchestrator_task(project, definition_of_done=definition_of_done, provider=provider)

        try:
            project_path = resolve_project_path(
                project, project_paths=project_paths, projects_root=projects_root
            )
        except ProjectPathError as exc:
            raise OrchestratorProcessError(str(exc)) from exc

        spec_path = write_spec_file(spec_dir, task)

        full_command = list(command) + [
            "--project", project_path,
            "--goal", task.task,
            "--spec", str(spec_path),
            "--agent", provider,
        ]

        started_at = clock()

        try:
            completed = subprocess_run(full_command)
        except Exception as exc:  # noqa: BLE001 - any spawn/timeout failure
            retry_after = detect_limit(exc)
            if retry_after is not None:
                return _mark_limited_result(provider_registry, provider, project, retry_after, str(exc))
            raise OrchestratorProcessError(f"failed to run ai-orchestrator: {exc}") from exc

        combined_output = "\n".join(
            part for part in (completed.stdout, completed.stderr) if part
        )

        if completed.returncode != 0:
            failure = RuntimeError(combined_output.strip() or f"exit code {completed.returncode}")
            retry_after = detect_limit(failure)
            if retry_after is not None:
                return _mark_limited_result(provider_registry, provider, project, retry_after, str(failure))
            raise OrchestratorProcessError(
                f"ai-orchestrator exited {completed.returncode}: {combined_output.strip()}"
            )

        try:
            payload = read_outbox(outbox_dir, task.project_name, started_at)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            raise OrchestratorProcessError(f"could not read ai-orchestrator outbox result: {exc}") from exc

        reported_limit = payload.get("limit_hit") or payload.get("session_limit")
        if reported_limit:
            retry_seconds = payload.get("retry_after_seconds")
            retry_after = (
                timedelta(seconds=retry_seconds)
                if retry_seconds
                else (detect_limit(RuntimeError(str(reported_limit))) or _DEFAULT_LIMIT_BACKOFF)
            )
            return _mark_limited_result(
                provider_registry,
                provider,
                project,
                retry_after,
                str(reported_limit),
                checkpoint=payload.get("checkpoint", project.checkpoint),
            )

        result: dict = {}
        for key in ("checkpoint", "last_output", "next_step", "stop_reason", "status"):
            if key in payload:
                result[key] = payload[key]
        if "status" not in result:
            result["status"] = "done" if payload.get("done") else "in_progress"
        return result

    return run_fn
