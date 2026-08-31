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

- ``project_paths`` is the explicit identity-to-checkout allowlist used
  to map a Trello-backed ``ProjectRecord`` safely (see
  ``resolve_project_path``). The legacy ``projects_root`` setting is
  accepted for configuration compatibility but never used to infer a path.
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

from .dod_validator import (
    RunCommand,
    default_run_command,
    get_git_head,
    get_git_status,
    validate_project_dod,
)
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
# --model/--run-id when a model is configured) -> a
# subprocess.CompletedProcess-like object with
# .returncode, .stdout, .stderr. Injectable so tests never spawn a real
# process and callers can point at any ai-orchestrator invocation shape.
SubprocessFn = Callable[[list], "subprocess.CompletedProcess"]

# (outbox_dir, project_name, run_id) -> the parsed result JSON, matched
# strictly by run_id. Injectable so tests can fake the outbox without
# touching the filesystem.
ReadOutboxFn = Callable[[str, str, str], dict]

_DEFAULT_LIMIT_BACKOFF = timedelta(minutes=30)
_PROVIDER_FAILURE_BACKOFF = timedelta(minutes=30)
_DEFAULT_SPEC_DIR = "specs"
_RUNTIME_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "AI_PROJECT_RUNTIME.md"
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
    """Normalize an explicit project identity without interpreting it.

    Priority-looking text is deliberately preserved: allowlist keys are
    identities, not card titles, and stripping a ``P0``-``P5`` prefix could
    make an unrelated title-shaped key collide with a real identity.
    """
    return re.sub(r"\s+", " ", name).strip().casefold()


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

    The card must carry one stable ``project_key`` label. That identity is
    matched against the explicit ``project_paths`` allowlist after only
    case/whitespace normalization. Card titles, priorities, descriptions,
    slugs, and ``projects_root`` are deliberately never used as repository
    signals. Missing, unknown, or conflicting mappings fail closed.
    """
    project_paths = project_paths or {}
    if not project.project_key:
        raise ProjectPathError(
            f"card {project.name!r} has no project identity label; P0-P5 is priority only"
        )

    identity = _project_identity(project.project_key)
    matches = [
        path for key, path in project_paths.items()
        if _project_identity(key) == identity
    ]
    if not matches:
        raise ProjectPathError(
            f"unknown project identity {project.project_key!r} on card {project.name!r}; "
            "configure an exact AI_PM_PROJECT_PATHS identity mapping"
        )
    distinct = {str(Path(path).resolve()) for path in matches}
    if len(distinct) != 1:
        raise ProjectPathError(
            f"ambiguous project identity {project.project_key!r}: configured paths disagree"
        )
    return matches[0]


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
    runtime_contract = _RUNTIME_CONTRACT_PATH.read_text(encoding="utf-8").strip()
    lines = [
        f"# {task.project_name}", "", "## Goal", "", goal, "",
        "## Runtime Contract", "", runtime_contract, "",
        "## Definition of Done", "",
    ]
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


def _identity_setting(settings: Optional[dict], identity: Optional[str]):
    if not settings or not identity:
        return None
    normalized = _project_identity(identity)
    matches = [value for key, value in settings.items() if _project_identity(key) == normalized]
    if len(matches) > 1 and any(value != matches[0] for value in matches[1:]):
        raise OrchestratorProcessError(f"ambiguous controller-finalize setting for {identity!r}")
    return matches[0] if matches else None


def _finalization_kind(text: str) -> Optional[str]:
    value = re.sub(r"\s+", " ", text.casefold()).strip()
    if "post-commit" in value or "post commit" in value or "po commitu" in value:
        return "post_commit"
    if ("push" in value or "pushnout" in value) and (
        "remote" in value or "vzdálen" in value or "vzdalen" in value
    ):
        return "push"
    if "commit" in value and ("orchestr" in value or "schválen" in value or "schvalen" in value):
        return "commit"
    return None


def _finalization_indices(project: ProjectRecord) -> Optional[list[int]]:
    pending = [
        (index, item)
        for index, item in enumerate(project.dod)
        if item.phase == "implementation" and not item.checked
    ]
    if not pending or any(_finalization_kind(item.text) is None for _, item in pending):
        return None
    return [index for index, _ in pending]


def _finalization_needs_refresh(project: ProjectRecord, project_path: str, run_git: RunCommand) -> bool:
    """Detect a newer local HEAD than the card's stored finalization proof."""
    finalization = (project.checkpoint or {}).get("finalization")
    recorded_head = finalization.get("commit_hash") if isinstance(finalization, dict) else None
    if not recorded_head:
        return False
    current_head = get_git_head(project_path, run_git=run_git)
    return bool(current_head and current_head != recorded_head)


def _controller_finalization_is_verified(
    finalization: object,
    current_head: Optional[str],
    previous_head: Optional[str] = None,
) -> bool:
    """Accept a controller proof for a commit already present at audit time."""
    proof_is_current = (
        isinstance(finalization, dict)
        and finalization.get("status") == "completed"
        and finalization.get("done") is True
        and isinstance(finalization.get("committed"), bool)
        and finalization.get("clean") is True
        and finalization.get("tests_passed") is True
        and finalization.get("pushed") is True
        and bool(current_head)
        and finalization.get("commit_hash") == current_head
        and finalization.get("remote_commit") == current_head
    )
    if not proof_is_current:
        return False
    # At the controller boundary we know both sides of the finalizer call.
    # A claim that it created a commit is only evidence when HEAD actually
    # advanced.  Audit-time validation omits previous_head because it consumes
    # an already-persisted proof and must not demand a second commit.
    if previous_head:
        head_changed = current_head != previous_head
        return head_changed is (finalization.get("committed") is True)
    return True


def _terminal_finalization_issue(
    finalization: object,
    current_head: Optional[str],
    project_path: str,
    run_git: RunCommand,
) -> Optional[str]:
    """Reject a dirty checkout that has no controller finalization proof.

    ``Testování -> Hotovo`` is a terminal lifecycle transition.  A successful
    provider/audit response is not enough when the checkout still contains
    changes: without this guard a card can become ``Hotovo`` while its work is
    neither committed nor covered by the controller's backup/remote proof.
    Clean checkouts (for example a research-only card) do not need a no-op
    commit, but an unreadable Git status is fail-closed.
    """
    if _controller_finalization_is_verified(finalization, current_head):
        return None
    status_ok, status = get_git_status(project_path, run_git=run_git)
    if not status_ok:
        return f"terminální finalizace nelze ověřit: git status selhal ({status})"
    if status.strip():
        return (
            "terminální controller finalizace je povinná před Hotovo: "
            "repozitář je dirty a chybí ověřený commit/backup/remote důkaz"
        )
    return None


def _post_completion_finalization_allowed(project: ProjectRecord) -> bool:
    """Return whether this card explicitly defers controller finalization.

    A narrowly-scoped exception is needed for work that was already live
    verified while the checkout was clean/controlled, but whose controller
    commit/backup is intentionally performed after the card reaches Hotovo.
    The flag is card-owned contract data, requires explicit human approval and
    never changes who may issue the audit verdict.
    """
    policy = project.extra_data.get("completion_policy")
    return (
        isinstance(policy, dict)
        and policy.get("mode") == "post_done_finalization"
        and policy.get("human_approved") is True
        and policy.get("controller_owner") == "ai-orchestrator"
    )


def _controller_finalize(
    project: ProjectRecord,
    project_path: str,
    task: OrchestratorTask,
    run_id: str,
    command: list,
    finalize_paths: Optional[dict],
    allowed_push_remotes: Optional[dict],
    subprocess_run: SubprocessFn,
    indices: list[int],
    run_git: RunCommand,
) -> dict:
    paths = _identity_setting(finalize_paths, project.project_key) or []
    allowed_remote = _identity_setting(allowed_push_remotes, project.project_key)
    full_command = list(command) + [
        "--project", project_path,
        "--run-id", run_id,
        "--goal", task.task,
        "--push",
    ]
    for path in paths:
        full_command.extend(["--path", path])
    if allowed_remote:
        full_command.extend(["--allowed-remote", allowed_remote])
    previous_head = get_git_head(project_path, run_git=run_git)
    if not previous_head:
        return {
            "status": "blocked",
            "stop_reason": "controller finalization cannot verify repository HEAD before execution",
        }
    try:
        completed = subprocess_run(full_command)
    except Exception as exc:  # noqa: BLE001 - finalization is a governed boundary
        return {
            "status": "blocked", "stop_reason": f"controller finalization failed to start: {exc}",
        }
    try:
        payload = json.loads((completed.stdout or "").strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {
            "status": "blocked",
            "error": (completed.stderr or completed.stdout or "invalid finalizer output").strip(),
        }
    if not _controller_finalization_is_verified(
        payload,
        get_git_head(project_path, run_git=run_git),
        previous_head=previous_head,
    ):
        reason = payload.get("error", "controller finalization did not complete") if isinstance(payload, dict) else "invalid finalizer result"
        return {
            "status": "blocked",
            "stop_reason": (
                f"controller finalization did not provide a complete verified proof: {reason}"
            ),
            "finalization": payload,
        }
    checkpoint = dict(project.checkpoint or {})
    completed_indices = set(checkpoint.get("completed_dod_indices") or [])
    completed_indices.update(indices)
    checkpoint["completed_dod_indices"] = sorted(completed_indices)
    checkpoint["finalization"] = payload
    return {
        "status": "done", "checkpoint": checkpoint,
        "last_output": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "next_step": "ai-orchestrator audit",
        "stop_reason": "controller commit, clean status and verified remote completed",
        "finalization": payload,
    }


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
    run_git: Optional[RunCommand] = None,
    finalize_command: Optional[list] = None,
    finalize_paths: Optional[dict] = None,
    allowed_push_remotes: Optional[dict] = None,
    use_provider_failover: bool = False,
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
    git_cmd = run_git or default_run_command
    if subprocess_run is None:
        subprocess_run = functools.partial(_default_subprocess_run, timeout=timeout_seconds)

    def run_fn(project: ProjectRecord, provider: str) -> dict:
        # The PM scheduler selects the first currently available provider for
        # visibility and priority. Production dispatch must still let
        # ai-orchestrator try the complete ordered chain in the same tick;
        # otherwise a Hermes stream failure only becomes an error and the next
        # tick starts from Hermes again. Tests and explicit callers retain the
        # old single-agent behavior unless they opt in.
        agent_name = "auto" if use_provider_failover else map_provider_to_agent(provider, provider_agent_map)
        selected_model = None if use_provider_failover else provider_registry.selected_model(provider)
        task = build_orchestrator_task(project, definition_of_done=definition_of_done, provider=agent_name)

        try:
            project_path = resolve_project_path(
                project, project_paths=project_paths, projects_root=projects_root
            )
        except ProjectPathError as exc:
            raise OrchestratorProcessError(str(exc)) from exc

        checkout = Path(project_path)
        if not checkout.is_dir():
            raise OrchestratorProcessError(
                f"configured repository path for {project.project_key!r} does not exist "
                f"or is not a directory: {project_path}"
            )

        run_id = run_id_fn()
        initial_head = get_git_head(project_path, run_git=git_cmd)
        existing_finalization = (project.checkpoint or {}).get("finalization")
        finalization_verified = _controller_finalization_is_verified(
            existing_finalization, initial_head
        )
        finalization_indices = _finalization_indices(project)
        # Do not run the controller finalizer at dispatch time for an ordinary
        # implementation card.  It is a terminal handoff for the explicit
        # commit/push tail only; invoking it before the agent gets a first
        # implementation iteration makes every fresh card spend its tick on
        # controller tests and can deadlock it before any work starts.
        if finalize_command and finalization_indices is not None and not finalization_verified:
            return _controller_finalize(
                project, project_path, task, run_id, finalize_command,
                finalize_paths, allowed_push_remotes, subprocess_run,
                finalization_indices or [], git_cmd,
            )

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
        ]
        if selected_model:
            full_command += ["--model", selected_model]
        full_command += [
            "--run-id", run_id,
            "--implementation-only",
            "--no-commit",
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
            "active_model",
            "model",
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
        provider_sequence = result.get("provider_sequence")
        active_provider = result.get("active_provider")
        # A production auto run may start with Hermes and finish through a
        # later provider. Persist the failed head as a temporary ERROR so the
        # next PM tick does not repeat the same expensive failure immediately;
        # this is a recheck backoff, not a permanent removal of Hermes.
        if (
            use_provider_failover
            and provider == "hermes"
            and isinstance(provider_sequence, list)
            and "hermes" in provider_sequence
            and len(provider_sequence) > 1
            and active_provider != "hermes"
        ):
            provider_registry.mark_error(
                "hermes",
                "Hermes selhal; failover na dalšího providera: "
                + str(result.get("stop_reason") or "provider failover"),
                retry_after=_PROVIDER_FAILURE_BACKOFF,
                checkpoint=result.get("checkpoint", project.checkpoint),
            )
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


def _audit_reject_target(project: ProjectRecord, rejected_indices: list[int]) -> str:
    """Route a rejected verdict according to the rejected DoD phases.

    This does not create a verdict. ai-orchestrator remains the sole audit
    authority; the PM only prevents an audit-only failure from reopening an
    already-complete implementation loop. Any implementation item keeps the
    historical ``in_progress`` route.
    """
    indices = sorted({index for index in (rejected_indices or []) if isinstance(index, int)})
    if indices and all(
        0 <= index < len(project.dod) and project.dod[index].phase == "audit"
        for index in indices
    ):
        return "testing"
    return "in_progress"


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
    run_git: Optional[RunCommand] = None,
    use_provider_failover: bool = False,
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
    git_cmd = run_git or default_run_command
    if subprocess_run is None:
        subprocess_run = functools.partial(_default_subprocess_run, timeout=timeout_seconds)

    def audit_run_fn(project: ProjectRecord, provider: str) -> dict:
        agent_name = "auto" if use_provider_failover else map_provider_to_agent(provider, provider_agent_map)
        selected_model = None if use_provider_failover else provider_registry.selected_model(provider)
        task = build_audit_task(project, provider=agent_name)

        try:
            project_path = resolve_project_path(
                project, project_paths=project_paths, projects_root=projects_root
            )
        except ProjectPathError as exc:
            raise OrchestratorProcessError(str(exc)) from exc

        checkout = Path(project_path)
        if not checkout.is_dir():
            raise OrchestratorProcessError(
                f"configured repository path for {project.project_key!r} does not exist "
                f"or is not a directory: {project_path}"
            )

        initial_head = get_git_head(project_path, run_git=git_cmd)
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
        ]
        if selected_model:
            full_command += ["--model", selected_model]
        full_command += [
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
        evidence = payload.get("last_output") or (last_iteration.get("test_output") if isinstance(last_iteration, dict) else None) or payload.get("evidence")

        # A Testování pass audits the already-finalized implementation. A
        # controller-owned commit is therefore evidence from the preceding
        # PM phase, not a second commit that must be created during audit.
        finalization = (project.checkpoint or {}).get("finalization")
        controller_finalization_verified = _controller_finalization_is_verified(finalization, initial_head)
        if controller_finalization_verified:
            controller_evidence = json.dumps(finalization, ensure_ascii=False, separators=(",", ":"))
            controller_summary = (
                "Controller finalization verified: "
                f"commit {finalization['commit_hash']}; clean working tree; "
                "tests passed; push passed; "
                f"remote HEAD {finalization['remote_commit']}."
            )
            evidence = "\n".join(
                part for part in (evidence, controller_summary, controller_evidence) if part
            )

        # Perform fail-closed validation of all DoD items against repository state and evidence
        post_completion_finalization = _post_completion_finalization_allowed(project)
        report = validate_project_dod(
            project,
            repo_path=project_path,
            initial_head=initial_head,
            evidence=evidence,
            run_git=git_cmd,
            expected_new_commit=not controller_finalization_verified and not post_completion_finalization,
            allow_dirty_checkout=post_completion_finalization,
        )

        if audit_performed and not audit_protocol_error and not rejected_indices and payload.get("status") == "completed":
            if report.is_valid:
                verdict = AUDIT_VERDICT_ACCEPTED
                return {
                    "verdict": verdict,
                    "evidence": evidence,
                    "usage": payload.get("usage"),
                    **{
                        key: payload[key]
                        for key in ("active_provider", "active_model", "model", "provider_sequence")
                        if key in payload
                    },
                }
            else:
                detail = report.rejection_summary or "DoD validace selhala"
                return {
                    "verdict": AUDIT_VERDICT_REJECTED,
                    "reason": f"ai-orchestrator audit rejected DoD index(es) {report.rejected_indices}: {detail}",
                    "evidence": evidence,
                    "reject_target": _audit_reject_target(project, report.rejected_indices),
                    "rejected_indices": report.rejected_indices,
                    "usage": payload.get("usage"),
                    **{
                        key: payload[key]
                        for key in ("active_provider", "active_model", "model", "provider_sequence")
                        if key in payload
                    },
                }
        if audit_performed and rejected_indices:
            all_rejected = sorted(set(list(rejected_indices) + report.rejected_indices))
            detail = report.rejection_summary or (last_iteration.get("note") if isinstance(last_iteration, dict) else None) or payload.get("stop_reason") or "ai-orchestrator audit rejected the implementation"
            audit_evidence = "\n".join(
                part for part in (
                    evidence,
                    last_iteration.get("note") if isinstance(last_iteration, dict) else None,
                    payload.get("last_output"),
                ) if part
            )
            return {
                "verdict": AUDIT_VERDICT_REJECTED,
                "reason": f"ai-orchestrator audit rejected DoD index(es) {all_rejected}: {detail}",
                "evidence": audit_evidence,
                "reject_target": _audit_reject_target(project, all_rejected),
                "rejected_indices": all_rejected,
                "usage": payload.get("usage"),
                **{
                    key: payload[key]
                    for key in ("active_provider", "active_model", "model", "provider_sequence")
                    if key in payload
                },
            }
        verdict = payload.get("verdict")
        if verdict not in (AUDIT_VERDICT_ACCEPTED, AUDIT_VERDICT_REJECTED):
            raise AuditVerdictMissingError(
                f"ai-orchestrator audit outbox result for run_id={run_id!r} carries no completed "
                "independent audit verdict; refusing to guess an accepted/rejected outcome"
            )

        if verdict == AUDIT_VERDICT_ACCEPTED and not report.is_valid:
            return {
                "verdict": AUDIT_VERDICT_REJECTED,
                "reason": f"ai-orchestrator audit rejected DoD index(es) {report.rejected_indices}: {report.rejection_summary}",
                "evidence": evidence,
                "reject_target": _audit_reject_target(project, report.rejected_indices),
                "rejected_indices": report.rejected_indices,
                "usage": payload.get("usage"),
                **{
                    key: payload[key]
                    for key in ("active_provider", "active_model", "model", "provider_sequence")
                    if key in payload
                },
            }

        terminal_issue = None if post_completion_finalization else _terminal_finalization_issue(
            finalization, initial_head, project_path, git_cmd
        )
        if verdict == AUDIT_VERDICT_ACCEPTED and terminal_issue:
            return {
                "verdict": AUDIT_VERDICT_REJECTED,
                "reason": terminal_issue,
                "evidence": evidence,
                "reject_target": "in_progress",
                "usage": payload.get("usage"),
                **{
                    key: payload[key]
                    for key in ("active_provider", "active_model", "model", "provider_sequence")
                    if key in payload
                },
            }

        result: dict = {"verdict": verdict, "reason": payload.get("reason")}
        if "evidence" in payload or evidence:
            result["evidence"] = payload.get("evidence") or evidence
        if "reject_target" in payload:
            result["reject_target"] = payload["reject_target"]
        elif verdict == AUDIT_VERDICT_REJECTED:
            result["reject_target"] = _audit_reject_target(project, report.rejected_indices)
            result["rejected_indices"] = report.rejected_indices
        if "checkpoint" in payload:
            result["checkpoint"] = payload["checkpoint"]
        for key in ("active_provider", "active_model", "model", "provider_sequence", "usage"):
            if key in payload:
                result[key] = payload[key]
        return result

    return audit_run_fn
