"""Task type + complexity classification rules (design layer).

Scope: Inbox request "AI Project Manager / ai-orchestrator - pravidla
klasifikace" (see PROJECT_AUDIT_ROADMAP.md section 8.7). This module answers,
for at least Inbox planning/implementation/audit and low/medium/high
complexity, which providers are allowed and which model quality tier should
be requested.

This is an additive rules layer only, exactly like ``ProviderRegistry.
model_for_task``/``model_for_tier`` (``providers.py``): nothing in
``orchestrator_runner.py`` calls it yet, so existing Inbox planning,
implementation and audit dispatch behavior is unchanged. A provider
allowlist here never widens what a task type may already use elsewhere
(e.g. it does not override the separate, capability-limit filter audit
dispatch applies per project/scope - see PROJECT_AUDIT_ROADMAP.md 8.2).

The Inbox-planning forbidden-provider set is imported from ``inbox.py``
(``INBOX_PLANNING_FORBIDDEN_PROVIDERS``) rather than re-declared, so this
stays a single additional read-only view instead of a third independent
copy of that policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Tuple

from .inbox import INBOX_PLANNING_FORBIDDEN_PROVIDERS
from .providers import (
    MODEL_TIER_BALANCED,
    MODEL_TIER_ECONOMICAL,
    MODEL_TIER_QUALITY,
    TASK_AUDIT,
    TASK_IMPLEMENTATION,
    TASK_INBOX_PLANNING,
    TASK_TYPES,
)

COMPLEXITY_LOW = "low"
COMPLEXITY_MEDIUM = "medium"
COMPLEXITY_HIGH = "high"
COMPLEXITIES = (COMPLEXITY_LOW, COMPLEXITY_MEDIUM, COMPLEXITY_HIGH)


@dataclass(frozen=True)
class TaskClassification:
    """Result of :func:`classify_task`: policy for one (task_type, complexity) pair."""

    task_type: str
    complexity: str
    forbidden_providers: FrozenSet[str]
    model_tier: str

    def allowed_providers(self, base_order: Tuple[str, ...]) -> Tuple[str, ...]:
        """Filter a provider order down to what this classification permits.

        ``base_order`` stays the single source of overall provider priority
        (``AI_PM_PROVIDERS``/``AI_PM_PROVIDERS_FOR_PROJECT``/
        ``INBOX_PLANNER_PROVIDERS``); this only removes entries this task
        type must never use, it never adds or reorders providers.
        """
        return tuple(
            candidate
            for candidate in base_order
            if str(candidate).strip().casefold() not in self.forbidden_providers
        )


# task_type -> complexity -> model quality tier.
#
# Inbox planning is a read-only classification pass over a single card, so
# it always stays on the economical tier regardless of how complex the
# described work turns out to be (matches the existing model_for_task/
# _inbox_model_hint behavior: index 0 for every Inbox planning call).
# Audit is the independent correctness gate, so it always requests the
# quality tier regardless of complexity (matches model_for_task: index -1
# for every audit). Implementation is the one task type where complexity
# actually changes the requested tier, scaling from economical to quality.
_MODEL_TIER_RULES = {
    TASK_INBOX_PLANNING: {complexity: MODEL_TIER_ECONOMICAL for complexity in COMPLEXITIES},
    TASK_IMPLEMENTATION: {
        COMPLEXITY_LOW: MODEL_TIER_ECONOMICAL,
        COMPLEXITY_MEDIUM: MODEL_TIER_BALANCED,
        COMPLEXITY_HIGH: MODEL_TIER_QUALITY,
    },
    TASK_AUDIT: {complexity: MODEL_TIER_QUALITY for complexity in COMPLEXITIES},
}

# task_type -> providers that are fail-closed forbidden regardless of
# complexity. Only Inbox planning has a task-type-specific provider
# restriction today (Hermes/Gemini, per the runtime contract); implementation
# and audit rely purely on normal availability (and, for audit, the separate
# per-scope capability-limit filter already applied in scheduler.py).
_FORBIDDEN_PROVIDER_RULES = {
    TASK_INBOX_PLANNING: INBOX_PLANNING_FORBIDDEN_PROVIDERS,
    TASK_IMPLEMENTATION: frozenset(),
    TASK_AUDIT: frozenset(),
}


def classify_task(task_type: str, complexity: str) -> TaskClassification:
    """Return the provider/model policy for ``task_type`` at ``complexity``.

    Fails closed (``ValueError``) on an unknown task type or complexity
    instead of guessing a default, matching ``ProviderRegistry.
    model_for_task``/``model_for_tier``.
    """
    if task_type not in TASK_TYPES:
        raise ValueError(f"unknown task_type {task_type!r}; expected one of {TASK_TYPES}")
    if complexity not in COMPLEXITIES:
        raise ValueError(f"unknown complexity {complexity!r}; expected one of {COMPLEXITIES}")
    return TaskClassification(
        task_type=task_type,
        complexity=complexity,
        forbidden_providers=_FORBIDDEN_PROVIDER_RULES[task_type],
        model_tier=_MODEL_TIER_RULES[task_type][complexity],
    )
