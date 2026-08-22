"""Real ``run_fn``: hands a project to the actual ai-orchestrator process
in autonomous mode and turns its result back into the dict shape
``runner.run_once`` expects.

The ai-orchestrator is a separate process (configured via
``config.OrchestratorConfig.command``); this module shells out to it
with the project's task/DoD/checkpoint as JSON and reads back JSON.
Any sign of a session/quota limit - a non-zero exit whose output looks
like a limit, a raised subprocess error, or an explicit
``limit_hit``/``session_limit`` field in the JSON payload - is routed
through ``providers.detect_limit`` and immediately recorded on the
``ProviderRegistry`` (state -> LIMITED, retry_after set) so the
scheduler will not try this provider again before then.
"""

from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from typing import Callable, Optional

from .models import ProjectRecord
from .orchestrator_handoff import build_orchestrator_task
from .providers import ProviderRegistry, detect_limit

# (command, task_json) -> a subprocess.CompletedProcess-like object with
# .returncode, .stdout, .stderr. Injectable so tests never spawn a real
# process and callers can point at any ai-orchestrator invocation shape.
SubprocessFn = Callable[[list, str], "subprocess.CompletedProcess"]

_DEFAULT_LIMIT_BACKOFF = timedelta(minutes=30)


def _default_subprocess_run(command: list, task_json: str) -> "subprocess.CompletedProcess":
    return subprocess.run(
        command,
        input=task_json,
        capture_output=True,
        text=True,
        check=False,
    )


class OrchestratorProcessError(RuntimeError):
    """Raised when ai-orchestrator fails in a way that is not a
    recognizable session/quota limit."""


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
    subprocess_run: SubprocessFn = _default_subprocess_run,
    definition_of_done: Optional[list] = None,
):
    """Build a ``run_fn(project, provider) -> dict`` that dispatches to the
    real ai-orchestrator in autonomous mode, carrying the project's
    goal/spec (``orchestrator_ready_task``/``main_task``), an explicit
    Definition of Done, and the current checkpoint to resume from.
    """

    def run_fn(project: ProjectRecord, provider: str) -> dict:
        task = build_orchestrator_task(project, definition_of_done=definition_of_done, provider=provider)
        task_json = json.dumps(task.to_dict())

        try:
            completed = subprocess_run(command, task_json)
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
            payload = json.loads(completed.stdout or "{}")
        except (ValueError, json.JSONDecodeError) as exc:
            raise OrchestratorProcessError(
                f"ai-orchestrator returned non-JSON output: {completed.stdout!r}"
            ) from exc

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
