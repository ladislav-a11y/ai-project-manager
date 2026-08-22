"""Scheduler/router: picks the next project to work on.

Selects the highest-priority (0-5, 5 = most urgent), unblocked project
that has at least one available provider. This is pure, local, in-memory
logic over already-fetched ProjectRecords and the ProviderRegistry's
cached state - it never calls a provider or spends an AI token just to
decide what to do next. Actual provider availability re-verification
happens separately via ``ProviderRegistry.recheck``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import ProjectRecord, ProjectStatus
from .providers import ProviderRegistry

# Statuses that represent projects the scheduler should never pick up,
# regardless of priority.
NOT_SCHEDULABLE_STATUSES = {ProjectStatus.DONE, ProjectStatus.PAUSED}


@dataclass
class SchedulingDecision:
    project: ProjectRecord
    provider: str


def is_schedulable(project: ProjectRecord) -> bool:
    if project.is_blocked:
        return False
    if project.status in NOT_SCHEDULABLE_STATUSES:
        return False
    return True


def pick_next_project(
    projects: list[ProjectRecord],
    provider_registry: ProviderRegistry,
    providers_for_project: Optional[dict] = None,
    default_providers: Optional[list[str]] = None,
) -> Optional[SchedulingDecision]:
    """Return the highest-priority unblocked project paired with an
    available provider, or None if nothing is schedulable right now.

    ``providers_for_project`` optionally maps project name -> list of
    acceptable provider names (falls back to ``default_providers``, or
    to every provider registered so far).
    """
    fallback_providers = default_providers or provider_registry.registered_names()

    candidates = [p for p in projects if is_schedulable(p)]
    # Highest priority first; stable-sort preserves input order as the
    # tie-break so earlier-listed (e.g. earlier-created) projects win.
    candidates.sort(key=lambda p: p.priority, reverse=True)

    for project in candidates:
        allowed_providers = (providers_for_project or {}).get(project.name, fallback_providers)
        for provider_name in allowed_providers:
            if provider_registry.is_available(provider_name):
                return SchedulingDecision(project=project, provider=provider_name)

    return None
