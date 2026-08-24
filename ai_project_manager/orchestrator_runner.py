"""Real ``run_fn``: hands a project to the actual ai-orchestrator process
in autonomous mode and turns its result back into the dict shape
``runner.run_once`` expects.

ai-orchestrator's real CLI (``orchestrator.py``) is an argv-driven
autonomous command - it does not read a task off stdin, and it does not
print a JSON result to stdout. A run is invoked as::

    <command...> --project <local-project-path> --goal <goal text> \
        --spec <path-to-spec-file> --agent <agent-name> --run-id <run-id>

and its result is written as a JSON file under an outbox directory
(``outbox/autonomous-<project-slug>*.json``), which this module reads
back after the process exits.

Several extra pieces make that contract work correctly and without any
hardcoded single project or path:

- ``project_paths``/``projects_root`` map a Trello-backed
  ``ProjectRecord`` onto the local checkout ai-orchestrator should
  operate on (see ``resolve_project_path``).
- ``spec_dir`` holds one stable, per-project Markdown spec file (named
  after the project's slug, overwritten every run) carrying the goal and
  a real ``- [ ] ...`` Definition of Done checklist ai-orchestrator can
  actually parse - see ``write_spec_file``/``parse_spec_markdown`` - so
  ai-orchestrator's own checkpoint resume keys off a spec whose identity
  never changes between runs.
- every run gets a fresh ``run_id`` (passed both as ``--run-id`` and
  embedded in the spec's metadata block) and the outbox result is only
  ever accepted if it echoes that same run_id back - never by "newest
  file matching the project's name" - so a stale or unrelated result
  file can never be mistaken for this run's outcome (see
  ``_read_outbox_result``).
- ``outbox_dir``/``spec_dir`` are resolved to absolute paths at
  ``build_run_fn`` time, so the directories ai-orchestrator's result is
  written to and read back from can never silently drift with the
  Project Manager process's own current working directory.

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
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Callable, Optional

from .models import ProjectRecord
from .orchestrator_handoff import OrchestratorTask, build_orchestrator_task
from .providers import ProviderRegistry, detect_limit

# command (argv, already including --project/--goal/--spec/--agent/
# --run-id) -> a subprocess.CompletedProcess-like object with
# .returncode, .stdout, .stderr. Injectable so tests never spawn a real
# process and callers can point at any ai-orchestrator invocation shape.
SubprocessFn = Callable[[list], "subprocess.CompletedProcess"]

# (outbox_dir, project_name, run_id) -> the parsed result JSON, matched
# strictly by run_id. Injectable so tests can fake the outbox without
# touching the filesystem.
ReadOutboxFn = Callable[[str, str, str], dict]

_DEFAULT_LIMIT_BACKOFF = timedelta(minutes=30)
_DEFAULT_SPEC_DIR = "specs"
_DEFAULT_OUTBOX_DIR = "outbox"

# Project Manager's own provider registry/locking/Trello state always
# uses its own stable provider name (e.g. "claude") - never anything
# translated. Only the argv/spec handed to the real ai-orchestrator CLI
# needs its agent identifier, which is not always the same string; this
# is the single place that translation happens.
DEFAULT_PROVIDER_AGENT_MAP = {"claude": "claude-code"}


def map_provider_to_agent(provider: str, provider_agent_map: Optional[dict] = None) -> str:
    """Translate a Project Manager provider name into the agent
    identifier ai-orchestrator's ``--agent`` expects. Unknown providers
    pass through unchanged."""
    mapping = provider_agent_map if provider_agent_map is not None else DEFAULT_PROVIDER_AGENT_MAP
    return mapping.get(provider, provider)


def _default_run_id() -> str:
    return uuid.uuid4().hex


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
    return Path(spec_dir) / f"{_slugify(project_name)}.md"


_CHECKPOINT_BLOCK_RE = re.compile(r"<!--\s*PM-CHECKPOINT\s*(.*?)-->", re.DOTALL)
_GOAL_SECTION_RE = re.compile(r"##\s*Goal\s*\n(.*?)\n##", re.DOTALL)
_DOD_ITEM_RE = re.compile(r"^- \[ \] (.+)$", re.MULTILINE)
_TITLE_RE = re.compile(r"^#\s*(.+)$", re.MULTILINE)
_CONSTRAINTS_SECTION_RE = re.compile(r"##\s*Constraints\s*\n(.*?)(?:\n##|\n<!--|\Z)", re.DOTALL)

# Committing is the orchestrator's job, run only after it has verified the
# agent's result - never the agent's own. This is spelled out to the agent
# in every spec so an autonomous run can never create its own git commits.
NO_COMMIT_INSTRUCTION = (
    "Do not run `git commit` (or `git commit --amend`) under any circumstances. "
    "Committing the result is the orchestrator's responsibility only, performed "
    "after it has verified your work."
)


def _render_spec_markdown(task: OrchestratorTask, run_id: str) -> str:
    """Render the goal and Definition of Done as a real Markdown
    checklist ai-orchestrator can parse (``- [ ] ...`` items), instead of
    a JSON object serialized onto a single DoD line. Checkpoint/provider/
    run_id metadata - needed for resume but not part of the human-
    readable checklist - travels in a fenced HTML comment, the same
    embedded-JSON-block pattern trello_sync.py uses for its data block."""
    lines = [f"# {task.project_name}", "", "## Goal", "", task.task.strip(), "", "## Definition of Done", ""]
    for item in task.definition_of_done:
        text = (item or "").strip()
        if text:
            lines.append(f"- [ ] {text}")
    lines += ["", "## Constraints", "", f"- {NO_COMMIT_INSTRUCTION}"]
    meta = {
        "run_id": run_id,
        "checkpoint": task.checkpoint,
        "provider": task.provider,
        "project_name": task.project_name,
    }
    lines += ["", "<!-- PM-CHECKPOINT", json.dumps(meta, indent=2, ensure_ascii=False), "-->", ""]
    return "\n".join(lines)


def parse_spec_markdown(text: str) -> dict:
    """Parse a spec file written by ``write_spec_file`` back into its
    parts. Used by tests (and any other consumer standing in for
    ai-orchestrator) to verify the Markdown DoD contract round-trips."""
    title_match = _TITLE_RE.search(text)
    project_name = title_match.group(1).strip() if title_match else ""

    goal_match = _GOAL_SECTION_RE.search(text)
    goal = goal_match.group(1).strip() if goal_match else ""

    dod = [item.strip() for item in _DOD_ITEM_RE.findall(text)]

    constraints_match = _CONSTRAINTS_SECTION_RE.search(text)
    constraints = constraints_match.group(1).strip() if constraints_match else ""

    meta = {}
    checkpoint_match = _CHECKPOINT_BLOCK_RE.search(text)
    if checkpoint_match:
        meta = json.loads(checkpoint_match.group(1))

    return {
        "project_name": project_name,
        "goal": goal,
        "definition_of_done": dod,
        "constraints": constraints,
        "checkpoint": meta.get("checkpoint", {}),
        "provider": meta.get("provider"),
        "run_id": meta.get("run_id"),
    }


def write_spec_file(spec_dir: str, task: OrchestratorTask, run_id: str) -> Path:
    """Write the goal and Definition of Done checklist (plus checkpoint/
    run_id metadata) for this run to the project's stable spec file and
    return its path."""
    path = spec_file_path(spec_dir, task.project_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_spec_markdown(task, run_id), encoding="utf-8")
    return path


def _read_outbox_result(outbox_dir: str, project_name: str, run_id: str) -> dict:
    """Read this run's result from the outbox, matched strictly by the
    run_id this run was launched with - never by "the newest file whose
    name matches the project" - so a stale result from a previous run,
    or one for a similarly-named project, can never be mistaken for this
    run's outcome."""
    slug = _slugify(project_name)
    outbox = Path(outbox_dir)
    candidates = sorted(
        outbox.glob(f"autonomous-{slug}*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"no outbox result found for project {project_name!r} run_id={run_id!r} in {outbox_dir!r} "
            f"(expected autonomous-{slug}*.json)"
        )
    for candidate in candidates:
        try:
            with candidate.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (ValueError, json.JSONDecodeError):
            continue
        if payload.get("run_id") == run_id:
            return payload
    raise FileNotFoundError(
        f"no outbox result matched run_id={run_id!r} for project {project_name!r} in {outbox_dir!r} "
        f"({len(candidates)} candidate file(s) found but none echoed this run's run_id - "
        "ai-orchestrator may not have finished yet, or wrote a stale/foreign result)"
    )


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
    run_id_fn: Callable[[], str] = _default_run_id,
    provider_agent_map: Optional[dict] = None,
):
    """Build a ``run_fn(project, provider) -> dict`` that dispatches to the
    real ai-orchestrator ``--project``/``--goal``/``--spec``/``--agent``/
    ``--run-id`` autonomous CLI, carrying the project's goal/DoD/checkpoint
    via a stable per-project Markdown spec file, and reads the result back
    from the outbox - matched strictly by this run's run_id - instead of
    assuming JSON on stdout.

    ``spec_dir``/``outbox_dir`` are resolved to absolute paths here, once,
    so the directories ai-orchestrator is told to write to and that this
    process later reads back from can never drift with either process's
    working directory changing between the two.
    """
    abs_spec_dir = str(Path(spec_dir).resolve())
    abs_outbox_dir = str(Path(outbox_dir).resolve())

    def run_fn(project: ProjectRecord, provider: str) -> dict:
        agent_name = map_provider_to_agent(provider, provider_agent_map)
        task = build_orchestrator_task(project, definition_of_done=definition_of_done, provider=agent_name)

        try:
            project_path = resolve_project_path(
                project, project_paths=project_paths, projects_root=projects_root
            )
        except ProjectPathError as exc:
            raise OrchestratorProcessError(str(exc)) from exc

        run_id = run_id_fn()
        spec_path = write_spec_file(abs_spec_dir, task, run_id)

        full_command = list(command) + [
            "--project", project_path,
            "--goal", task.task,
            "--spec", str(spec_path),
            "--agent", agent_name,
            "--run-id", run_id,
            
        ]

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
            payload = read_outbox(abs_outbox_dir, task.project_name, run_id)
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
