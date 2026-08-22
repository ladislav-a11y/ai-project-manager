"""Guards the orchestrator against repeated permission-denial / blocked
test-command loops.

If a run keeps failing with the *same* signature (an identical denied
permission, the exact command that keeps getting blocked, ...), retrying
it verbatim will never succeed and only burns tokens and time. This
tracks consecutive identical failures per project and reports when the
project should be halted instead of retried again, so the caller can
mark it blocked for a human rather than looping forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_MAX_REPEATS = 2


@dataclass
class OrchestratorGuard:
    """``max_repeats`` is how many times the *same* failure signature may
    repeat in a row before ``record_denial`` reports the project should
    be halted (so 2 means: original failure + 2 identical retries = 3rd
    strike halts it)."""

    max_repeats: int = DEFAULT_MAX_REPEATS
    _counts: dict = field(default_factory=dict)
    _last_signature: dict = field(default_factory=dict)

    def record_denial(self, project_name: str, signature: str) -> bool:
        """Record a failure (permission denial, blocked test command,
        etc.) for ``project_name`` identified by ``signature``. Returns
        True once that exact signature has repeated too many times in a
        row and the project should be halted rather than retried."""
        if self._last_signature.get(project_name) == signature:
            self._counts[project_name] = self._counts.get(project_name, 0) + 1
        else:
            self._last_signature[project_name] = signature
            self._counts[project_name] = 1
        return self._counts[project_name] > self.max_repeats

    def should_halt(self, project_name: str) -> bool:
        return self._counts.get(project_name, 0) > self.max_repeats

    def reset(self, project_name: str) -> None:
        """Clear the failure streak - call this after a successful run so
        a later, unrelated failure starts counting from zero."""
        self._counts.pop(project_name, None)
        self._last_signature.pop(project_name, None)
