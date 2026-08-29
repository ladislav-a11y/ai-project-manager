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

import functools
import json
import re
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Callable, Optional

from .models import ProjectRecord
from .orchestrator_handoff import (
    AUDIT_VERDICT_ACCEPTED,
    AUDIT_VERDICT_REJECTED,
    OrchestratorTask,
    build_audit_task,
    build_orchestrator_task,
)
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


def _default_subprocess_run(command: list, timeout: Optional[float] = None) -> "subprocess.CompletedProcess":
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


class OrchestratorProcessError(RuntimeError):
    """Raised when ai-orchestrator fails in a way that is not a
    recognizable session/quota limit."""


class ProjectPathError(RuntimeError):
    """Raised when a project cannot be mapped to a local checkout path."""


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "project"


# Card titles on the live board encode priority as a free-text prefix
# (e.g. "P5 - Station Agent", "P4 — Station Agent checkpoint") rather
# than only via the Trello label - see trello_sync.TITLE_PRIORITY_RE,
# which this mirrors for the same prefix shape. That prefix changes every
# time a card is re-prioritized and is never part of the project's
# identity, so it must not affect which local checkout a card resolves
# to.
_PRIORITY_PREFIX_RE = re.compile(r"^\s*P[0-5]\s*[-–—:]*\s*", re.IGNORECASE)


def _strip_priority_prefix(name: str) -> str:
    return _PRIORITY_PREFIX_RE.sub("", name, count=1)


def _project_identity(name: str) -> str:
    """The stable part of a card title used for path resolution: the
    priority prefix stripped, whitespace collapsed, case-folded.

    Deliberately not the full descriptive title - a card's wording is
    routinely edited (status notes appended, typos fixed, priority
    changed) while the underlying project stays the same, so matching
    must survive that instead of requiring the exact current title."""
    stripped = _strip_priority_prefix(name)
    return re.sub(r"\s+", " ", stripped).strip().casefold()


def _phrase_in(haystack: str, needle: str) -> bool:
    """Whether ``needle`` occurs in ``haystack`` on word boundaries -
    i.e. not merely as a substring inside a larger word (so a configured
    key like "Agent" does not spuriously match inside "Agentura")."""
    if not needle:
        return False
    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        before_ok = start == 0 or not haystack[start - 1].isalnum()
        after_ok = end == len(haystack) or not haystack[end].isalnum()
        if before_ok and after_ok:
            return True
        start = haystack.find(needle, start + 1)
    return False


def resolve_project_path(
    project: ProjectRecord,
    project_paths: Optional[dict] = None,
    projects_root: Optional[str] = None,
) -> str:
    """Map a Trello-backed ``ProjectRecord`` onto the local checkout
    ai-orchestrator's ``--project`` argument should point at.

    Resolution order (no single hardcoded path):
      1. an explicit per-project override in ``project_paths``, keyed by
         the card's exact, current ``project.name`` (e.g. from
         ``AI_PM_PROJECT_PATHS``) - preserved verbatim so a one-off card
         can always be pinned regardless of the rules below;
      2. ``project.project_key`` - a plain Trello label on the card (see
         ``trello_sync.project_key_from_labels``), looked up verbatim
         (case/whitespace-folded) as a ``project_paths`` key. This is the
         card's *stable* identity: unlike the card's title, a label is not
         free-form prose an author keeps rewriting, so this is the only
         mechanism here that does not depend in any way on what the title
         happens to say - a card titled "Izolace testovacich Slack
         notifikaci" with the "AI Project Manager" label still resolves
         correctly even though that phrase never appears in its title;
      3. (legacy/fallback, title-based) a ``project_paths`` key whose
         priority-stripped, case-folded form occurs as a whole phrase
         inside the card's own priority-stripped title - this only works
         when the title happens to mention the project's identity phrase,
         which is exactly the limitation step 2 exists to remove. When
         more than one key of the longest matching length points at
         different paths, resolution refuses to guess and raises instead;
      4. ``<projects_root>/<slug(identity)>`` when a shared root is
         configured (e.g. from ``AI_PM_PROJECTS_ROOT``).

    A project matching none of these raises loudly rather than silently
    guessing a path.
    """
    project_paths = project_paths or {}

    if project.name in project_paths:
        return project_paths[project.name]

    if project.project_key:
        key_identity = _project_identity(project.project_key)
        for key, path in project_paths.items():
            if _project_identity(key) == key_identity:
                return path

    identity = _project_identity(project.name)
    if identity:
        matches = []
        for key, path in project_paths.items():
            norm_key = _project_identity(key)
            if norm_key and _phrase_in(identity, norm_key):
                matches.append((len(norm_key), key, path))
        if matches:
            longest = max(length for length, _key, _path in matches)
            best = [m for m in matches if m[0] == longest]
            distinct_paths = {path for _length, _key, path in best}
            if len(distinct_paths) > 1:
                keys = ", ".join(repr(key) for _length, key, _path in best)
                raise ProjectPathError(
                    f"project {project.name!r} matches multiple equally-specific "
                    f"AI_PM_PROJECT_PATHS entries pointing at different repositories "
                    f"({keys}); make one entry more specific or add an exact "
                    "AI_PM_PROJECT_PATHS override for this card's current title"
                )
            return best[0][2]

    if projects_root:
        return str(Path(projects_root) / _slugify(identity or project.name))

    raise ProjectPathError(
        f"cannot resolve local path for project {project.name!r}: "
        "configure AI_PM_PROJECT_PATHS (per-project) or AI_PM_PROJECTS_ROOT (shared base dir)"
    )


def spec_file_path(
    spec_dir: str,
    project_name: str,
    project_identity: Optional[str] = None,
) -> Path:
    """Return the stable spec path for a project.

    ``project_name`` is a Trello card title and can change whenever priority or
    status wording is edited.  Prefer the persisted ``project_key`` identity
    when the caller has one; legacy cards retain their previous title-based
    path until they are assigned a key.
    """
    identity = project_identity.strip() if project_identity and project_identity.strip() else project_name
    return Path(spec_dir) / f"{_slugify(identity)}.md"


_CHECKPOINT_BLOCK_RE = re.compile(r"<!--\s*PM-CHECKPOINT\s*(.*?)-->", re.DOTALL)
_GOAL_SECTION_RE = re.compile(r"##\s*Goal\s*\n(.*?)\n##", re.DOTALL)
# Scoped to the "## Definition of Done" section only, not the whole file -
# a project's Goal text is very often the raw Trello card description
# verbatim (see orchestrator_handoff._goal_text), which can itself contain
# literal "- [ ] ..." checklist markdown. Matching "- [ ] ..." anywhere in
# the document would then double-count every DoD item (once from the DoD
# section, once again from inside the Goal section's own text).
_DOD_SECTION_RE = re.compile(r"##\s*Definition of Done\s*\n(.*?)(?:\n##|\n<!--|\Z)", re.DOTALL)
_DOD_ITEM_RE = re.compile(r"^- \[ \] (.+)$", re.MULTILINE)
_TITLE_RE = re.compile(r"^#\s*(.+)$", re.MULTILINE)
_CONSTRAINTS_SECTION_RE = re.compile(r"##\s*Constraints\s*\n(.*?)(?:\n##|\n<!--|\Z)", re.DOTALL)

# The checkpoint travels arbitrary agent-produced data across runs (a
# diff, partial output, ...) and can very plausibly contain the literal
# substring "-->", which would otherwise prematurely close this fenced
# HTML comment, truncate the embedded JSON, and corrupt the spec file
# handed to the real external ai-orchestrator process on every resume -
# the same failure mode fixed in trello_sync.py's PM-DATA block, applied
# here with the same reversible zero-width-space escape since we cannot
# control (or verify) how the external ai-orchestrator's own parser
# behaves once it receives a literal "-->" inside the comment body.
_ZWSP = "\u200b"


def _escape_comment_terminator(text: str) -> str:
    return text.replace("-->", f"--{_ZWSP}>")


def _unescape_comment_terminator(text: str) -> str:
    return text.replace(f"--{_ZWSP}>", "-->")


# Committing is the orchestrator's job, run only after it has verified the
# agent's result - never the agent's own. This is spelled out to the agent
# in every spec so an autonomous run can never create its own git commits.
NO_COMMIT_INSTRUCTION = (
    "Do not run `git commit` (or `git commit --amend`) under any circumstances. "
    "Committing the result is the orchestrator's responsibility only, performed "
    "after it has verified your work."
)
GOVERNANCE_INSTRUCTION = (
    "Trello is the only source of truth for task priority, lifecycle, DoD and completion. "
    "The control hierarchy is AI Project Manager -> ai-orchestrator -> agents. "
    "Only ai-orchestrator may perform the audit and issue an accepted/rejected verdict; "
    "agents implement and provide evidence, and AI Project Manager only enforces workflow."
)


def _goal_without_checklist_markers(goal: str) -> str:
    """Keep Trello task context in Goal without duplicating DoD checkboxes.

    ai-orchestrator parses checkbox lines across the complete spec, not only
    the dedicated Definition of Done section.  A raw Trello description often
    contains the same checklist, so render those Goal lines as ordinary
    bullets and leave the canonical checkboxes exclusively in DoD.
    """
    cleaned_lines = []
    for line in (goal or "").splitlines():
        # Trello often stores a compact ``DEFINITION OF DONE: [ ] ...``
        # line inside the goal and the same items are rendered again in the
        # canonical DoD section below. Drop that embedded copy entirely;
        # otherwise ai-orchestrator sees extra unmet items and may start an
        # implementation iteration during what should be an audit-only pass.
        if re.search(r"(?i)\b(?:definition\s+of\s+done|důvod)\s*:", line) and re.search(r"\[[ xX]\]", line):
            prefix = re.split(r"(?i)\b(?:definition\s+of\s+done|důvod)\s*:", line, maxsplit=1)[0].rstrip()
            if prefix:
                cleaned_lines.append(prefix)
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    return re.sub(r"(?m)^([ \t]*[-*][ \t]+)\[[ xX]\][ \t]+", r"\1", cleaned)


def _render_spec_markdown(task: OrchestratorTask, run_id: str) -> str:
    """Render the goal and Definition of Done as a real Markdown
    checklist ai-orchestrator can parse (``- [ ] ...`` items), instead of
    a JSON object serialized onto a single DoD line. Checkpoint/provider/
    run_id metadata - needed for resume but not part of the human-
    readable checklist - travels in a fenced HTML comment, the same
    embedded-JSON-block pattern trello_sync.py uses for its data block."""
    goal = _goal_without_checklist_markers(task.task.strip())
    lines = [f"# {task.project_name}", "", "## Goal", "", goal, "", "## Definition of Done", ""]
    completed_indices = {
        index for index in (task.checkpoint or {}).get("completed_dod_indices", [])
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0
    }
    for index, item in enumerate(task.definition_of_done):
        text = (item or "").strip()
        if text:
            marker = "x" if index in completed_indices else " "
            lines.append(f"- [{marker}] {text}")
    lines += [
        "", "## Constraints", "",
        f"- {GOVERNANCE_INSTRUCTION}",
        f"- {NO_COMMIT_INSTRUCTION}",
    ]
    meta = {
        "run_id": run_id,
        "mode": task.mode,
        "checkpoint": task.checkpoint,
        "provider": task.provider,
        "project_name": task.project_name,
        "governance": task.governance,
    }
    meta_body = _escape_comment_terminator(json.dumps(meta, indent=2, ensure_ascii=False))
    lines += ["", "<!-- PM-CHECKPOINT", meta_body, "-->", ""]
    return "\n".join(lines)


def parse_spec_markdown(text: str) -> dict:
    """Parse a spec file written by ``write_spec_file`` back into its
    parts. Used by tests (and any other consumer standing in for
    ai-orchestrator) to verify the Markdown DoD contract round-trips."""
    title_match = _TITLE_RE.search(text)
    project_name = title_match.group(1).strip() if title_match else ""

    goal_match = _GOAL_SECTION_RE.search(text)
    goal = goal_match.group(1).strip() if goal_match else ""

    dod_match = _DOD_SECTION_RE.search(text)
    dod_section = dod_match.group(1) if dod_match else ""
    dod = [item.strip() for item in _DOD_ITEM_RE.findall(dod_section)]

    constraints_match = _CONSTRAINTS_SECTION_RE.search(text)
    constraints = constraints_match.group(1).strip() if constraints_match else ""

    meta = {}
    checkpoint_match = _CHECKPOINT_BLOCK_RE.search(text)
    if checkpoint_match:
        meta = json.loads(_unescape_comment_terminator(checkpoint_match.group(1)))

    return {
        "project_name": project_name,
        "goal": goal,
        "definition_of_done": dod,
        "constraints": constraints,
        "checkpoint": meta.get("checkpoint", {}),
        "provider": meta.get("provider"),
        "run_id": meta.get("run_id"),
        "governance": meta.get("governance"),
        "mode": meta.get("mode"),
    }


def write_spec_file(
    spec_dir: str,
    task: OrchestratorTask,
    run_id: str,
    project_identity: Optional[str] = None,
) -> Path:
    """Write the goal and Definition of Done checklist (plus checkpoint/
    run_id metadata) for this run to the project's stable spec file and
    return its path."""
    path = spec_file_path(spec_dir, task.project_name, project_identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_spec_markdown(task, run_id), encoding="utf-8")
    return path


def _read_outbox_result(outbox_dir: str, project_name: str, run_id: str) -> dict:
    """Read this run's result from the outbox, matched strictly by the
    run_id this run was launched with - never by "the newest file whose
    name matches the project" - so a stale result from a previous run,
    or one for a similarly-named project, can never be mistaken for this
    run's outcome."""
    outbox = Path(outbox_dir)
    # ai-orchestrator's real contract is autonomous-<run-id>.json.  Prefer
    # that exact path, then scan the remaining autonomous results by embedded
    # run_id for compatibility with older/project-slug-named producers.
    direct = outbox / f"autonomous-{run_id}.json"
    candidates = [direct] if direct.exists() else []
    candidates.extend(
        path
        for path in sorted(
            outbox.glob("autonomous-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if path != direct
    )
    if not candidates:
        raise FileNotFoundError(
            f"no outbox result found for project {project_name!r} run_id={run_id!r} in {outbox_dir!r} "
            "(expected autonomous-<run-id>.json)"
        )
    for candidate in candidates:
        try:
            with candidate.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (ValueError, json.JSONDecodeError):
            continue
        # A truncated writer is not the only way an outbox artifact can be
        # unusable: valid JSON may still have the wrong top-level shape
        # (for example ``[]`` or ``null``).  Treat it like any other foreign
        # candidate instead of leaking AttributeError from ``payload.get``.
        if not isinstance(payload, dict):
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
    # For an implementation run, a provider/session limit is a recoverable
    # workflow wait: persist the checkpoint and retry time in PAUSED so Trello
    # visibly moves to "Čeká na AI". The audit caller deliberately ignores
    # this status field and keeps its card in Testování until a verdict exists.
    result: dict = {
        "status": "paused",
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
    subprocess_run: Optional[SubprocessFn] = None,
    timeout_seconds: Optional[float] = None,
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

    ``timeout_seconds`` (from ``AI_ORCHESTRATOR_TIMEOUT_SECONDS``) bounds
    the real subprocess call so a hung ai-orchestrator/agent process can
    never wedge the scheduler loop forever - a ``subprocess.TimeoutExpired``
    is just another exception the ``except Exception`` below already
    turns into a provider-limit result or an ``OrchestratorProcessError``.
    It is only applied to the real default subprocess call; a caller
    supplying its own ``subprocess_run`` (tests, or a different dispatch
    mechanism entirely) is responsible for its own timeout handling.
    """
    abs_spec_dir = str(Path(spec_dir).resolve())
    abs_outbox_dir = str(Path(outbox_dir).resolve())
    if subprocess_run is None:
        subprocess_run = functools.partial(_default_subprocess_run, timeout=timeout_seconds)

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
        # The card title is mutable (including its P0-P5 prefix).  Key the
        # reusable spec/checkpoint path by the persisted project identity so a
        # reprioritization or title edit cannot silently start a fresh run.
        spec_path = write_spec_file(
            abs_spec_dir,
            task,
            run_id,
            project_identity=project.project_key,
        )

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

        # Every legitimate autonomous status is written to the outbox before
        # the CLI exits.  Non-completed states intentionally use exit code 1,
        # so the outbox is authoritative and must be read before interpreting
        # the process return code as a crash.
        try:
            payload = read_outbox(abs_outbox_dir, task.project_name, run_id)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            if completed.returncode != 0:
                failure = RuntimeError(combined_output.strip() or f"exit code {completed.returncode}")
                retry_after = detect_limit(failure)
                if retry_after is not None:
                    return _mark_limited_result(
                        provider_registry, provider, project, retry_after, str(failure)
                    )
                raise OrchestratorProcessError(
                    f"ai-orchestrator exited {completed.returncode} and no matching outbox result was found: {exc}; "
                    f"output: {combined_output.strip()}"
                ) from exc
            raise OrchestratorProcessError(f"could not read ai-orchestrator outbox result: {exc}") from exc

        # Keep the boundary safe even when an embedding supplies a custom
        # read_outbox callback.  The remaining contract relies on mapping
        # operations and should report a controlled orchestrator error for a
        # malformed payload, not an implementation-detail AttributeError.
        if not isinstance(payload, dict):
            raise OrchestratorProcessError(
                "ai-orchestrator outbox result must be an object, got "
                f"{type(payload).__name__} for run_id={run_id!r}"
            )

        orchestrator_status = payload.get("status")
        reported_limit = payload.get("limit_hit") or payload.get("session_limit")
        if orchestrator_status == "waiting_for_provider" and not reported_limit:
            reported_limit = payload.get("error") or "all configured providers are limited"
        if reported_limit:
            retry_seconds = payload.get("retry_after_seconds")
            retry_after = (
                timedelta(seconds=retry_seconds)
                if retry_seconds is not None
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
        for key in (
            "checkpoint",
            "last_output",
            "next_step",
            "stop_reason",
            "active_provider",
            "provider_sequence",
            "usage",
        ):
            if key in payload:
                result[key] = payload[key]
        status_map = {
            "completed": "done",
            "running": "in_progress",
            "max_iterations": "in_progress",
            "blocked": "blocked",
            "protocol_error": "blocked",
            "error": "error",
        }
        if orchestrator_status in status_map:
            result["status"] = status_map[orchestrator_status]
        elif orchestrator_status in {"new", "ready", "in_progress", "paused", "done"}:
            result["status"] = orchestrator_status
        elif orchestrator_status is None:
            result["status"] = "done" if payload.get("done") else "in_progress"
        else:
            raise OrchestratorProcessError(
                f"ai-orchestrator returned unknown status {orchestrator_status!r} for run_id={run_id!r}"
            )
        if "stop_reason" not in result and result["status"] != "done":
            result["stop_reason"] = payload.get("error") or str(orchestrator_status)
        return result

    return run_fn


class AuditVerdictMissingError(OrchestratorProcessError):
    """Raised when ai-orchestrator's audit outbox result carries no
    ``verdict`` field.

    Audit authority belongs exclusively to ai-orchestrator (see
    orchestrator_handoff.apply_audit_verdict) - the Project Manager must
    never infer accepted/rejected from an absent field or any other
    signal, so a missing verdict is a hard error rather than a silent
    default.
    """


def build_audit_run_fn(
    provider_registry: ProviderRegistry,
    command: list,
    project_paths: Optional[dict] = None,
    projects_root: Optional[str] = None,
    spec_dir: str = _DEFAULT_SPEC_DIR,
    outbox_dir: str = _DEFAULT_OUTBOX_DIR,
    subprocess_run: Optional[SubprocessFn] = None,
    timeout_seconds: Optional[float] = None,
    read_outbox: ReadOutboxFn = _read_outbox_result,
    run_id_fn: Callable[[], str] = _default_run_id,
    provider_agent_map: Optional[dict] = None,
):
    """Build an ``audit_run_fn(project, provider) -> dict`` that dispatches
    a Testování card to ai-orchestrator's audit-only mode and reads back
    its accepted/rejected verdict.

    Mirrors ``build_run_fn``'s process/spec/outbox contract (same
    ``--project``/``--spec``/``--agent``/``--run-id`` argv shape). The real
    ai-orchestrator exposes audit as the internal audit phase of its supported
    ``autonomous`` command, not as a ``--mode audit`` CLI flag. Testování
    cards arrive with their implementation DoD already checked, so the
    orchestrator skips the executor and performs its test/audit phase; PM
    also forces ``--no-commit``. This path never
    interprets a "status" field as a lifecycle transition - the only
    thing this ever returns for a real result is the verdict payload
    (``verdict``/``reason``/``evidence``/``reject_target``) exactly as
    ai-orchestrator reported it, for ``runner.run_once_audit`` to apply
    via ``orchestrator_handoff.apply_audit_verdict``. A provider/session
    limit is handled the same way as the autonomous path, but leaves the
    card's status untouched (still TESTING, still awaiting audit) since a
    limit is not a verdict.
    """
    abs_spec_dir = str(Path(spec_dir).resolve())
    abs_outbox_dir = str(Path(outbox_dir).resolve())
    if subprocess_run is None:
        subprocess_run = functools.partial(_default_subprocess_run, timeout=timeout_seconds)

    def audit_run_fn(project: ProjectRecord, provider: str) -> dict:
        agent_name = map_provider_to_agent(provider, provider_agent_map)
        task = build_audit_task(project, provider=agent_name)

        try:
            project_path = resolve_project_path(
                project, project_paths=project_paths, projects_root=projects_root
            )
        except ProjectPathError as exc:
            raise OrchestratorProcessError(str(exc)) from exc

        run_id = run_id_fn()
        spec_path = write_spec_file(
            abs_spec_dir,
            task,
            run_id,
            project_identity=project.project_key,
        )

        full_command = list(command) + [
            "--project", project_path,
            "--goal", task.task,
            "--spec", str(spec_path),
            "--agent", agent_name,
            "--run-id", run_id,
            # Testování is an audit gate, not another implementation loop.
            # One autonomous iteration is enough to run the configured tests
            # and the independent audit; a rejection must return to Trello
            # with feedback instead of spending nine more provider calls.
            "--max-iterations", "1",
            "--no-commit",
        ]

        try:
            completed = subprocess_run(full_command)
        except Exception as exc:  # noqa: BLE001 - any spawn/timeout failure
            retry_after = detect_limit(exc)
            if retry_after is not None:
                return _mark_limited_result(provider_registry, provider, project, retry_after, str(exc))
            raise OrchestratorProcessError(f"failed to run ai-orchestrator audit: {exc}") from exc

        combined_output = "\n".join(
            part for part in (completed.stdout, completed.stderr) if part
        )

        try:
            payload = read_outbox(abs_outbox_dir, task.project_name, run_id)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            if completed.returncode != 0:
                failure = RuntimeError(combined_output.strip() or f"exit code {completed.returncode}")
                retry_after = detect_limit(failure)
                if retry_after is not None:
                    return _mark_limited_result(
                        provider_registry, provider, project, retry_after, str(failure)
                    )
                raise OrchestratorProcessError(
                    f"ai-orchestrator audit exited {completed.returncode} and no matching outbox "
                    f"result was found: {exc}; output: {combined_output.strip()}"
                ) from exc
            raise OrchestratorProcessError(f"could not read ai-orchestrator audit outbox result: {exc}") from exc

        if not isinstance(payload, dict):
            raise OrchestratorProcessError(
                "ai-orchestrator audit outbox result must be an object, got "
                f"{type(payload).__name__} for run_id={run_id!r}"
            )

        reported_limit = payload.get("limit_hit") or payload.get("session_limit")
        if reported_limit:
            retry_seconds = payload.get("retry_after_seconds")
            retry_after = (
                timedelta(seconds=retry_seconds)
                if retry_seconds is not None
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

        # ``orchestrator.py autonomous`` has no external verdict field. Its
        # own independent audit is represented in the final iteration log;
        # translate that ai-orchestrator-owned result without trusting the
        # implementation agent's text or the top-level status alone.
        iterations = payload.get("iterations")
        last_iteration = iterations[-1] if isinstance(iterations, list) and iterations else {}
        audit_performed = bool(last_iteration.get("audit_performed")) if isinstance(last_iteration, dict) else False
        rejected_indices = (
            last_iteration.get("audit_rejected_indices")
            if isinstance(last_iteration, dict)
            else None
        )
        audit_protocol_error = bool(last_iteration.get("audit_protocol_error")) if isinstance(last_iteration, dict) else False
        if audit_performed and not audit_protocol_error and not rejected_indices and payload.get("status") == "completed":
            verdict = AUDIT_VERDICT_ACCEPTED
            evidence = payload.get("last_output") or last_iteration.get("test_output")
            return {"verdict": verdict, "evidence": evidence, "usage": payload.get("usage")}
        if audit_performed and rejected_indices:
            detail = last_iteration.get("note") or payload.get("stop_reason") or "ai-orchestrator audit rejected the implementation"
            return {
                "verdict": AUDIT_VERDICT_REJECTED,
                "reason": f"ai-orchestrator audit rejected DoD index(es) {rejected_indices}: {detail}",
                "evidence": last_iteration.get("test_output"),
                "reject_target": "in_progress",
                "usage": payload.get("usage"),
            }
        verdict = payload.get("verdict")
        if verdict not in (AUDIT_VERDICT_ACCEPTED, AUDIT_VERDICT_REJECTED):
            raise AuditVerdictMissingError(
                f"ai-orchestrator audit outbox result for run_id={run_id!r} carries no completed "
                "independent audit verdict; refusing to guess an accepted/rejected outcome"
            )

        result: dict = {"verdict": verdict, "reason": payload.get("reason")}
        if "evidence" in payload:
            result["evidence"] = payload["evidence"]
        if "reject_target" in payload:
            result["reject_target"] = payload["reject_target"]
        if "checkpoint" in payload:
            result["checkpoint"] = payload["checkpoint"]
        if "usage" in payload:
            result["usage"] = payload["usage"]
        return result

    return audit_run_fn
