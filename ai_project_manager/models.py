"""The unified project record.

Trello is the single source of truth for a project's state, priority,
task, feedback, next step and last result (see trello_sync.py for the
mapping between a Trello card and a ProjectRecord). ProjectRecord is a
plain, serializable snapshot derived from that source of truth -- it is
never an independent store that could drift from Trello.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


MIN_PRIORITY = 0
# Integer P5..P0 remains the public banding. Decimal subpriorities below 6
# (for example P2.01) distinguish children of one split Inbox request
# without changing the meaning that a higher number wins.
MAX_PRIORITY = 5.999999


class ProjectStatus(str, Enum):
    """Project lifecycle state. Backed 1:1 by the Trello list the card sits in."""

    INBOX = "inbox"
    NEW = "new"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    TESTING = "testing"
    PAUSED = "paused"
    BLOCKED = "blocked"
    DONE = "done"
    ERROR = "error"


@dataclass
class GitHubRef:
    """Reference to a GitHub repository. A pointer only, not a duplicated
    source of truth -- the repo's own state (issues, branches, commits)
    is read live from GitHub, not mirrored here."""

    repo_url: str
    default_branch: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["GitHubRef"]:
        if not data:
            return None
        return cls(repo_url=data["repo_url"], default_branch=data.get("default_branch"))


@dataclass
class DoDItem:
    """One Definition-of-Done checklist entry, carried on
    ``ProjectRecord.dod``.

    Persisted through the Trello structured block (see
    ``trello_sync.card_updates_from_project``) so a card's exact
    checklist -- every item, in the deterministic order it was first
    read in, and which of them are already checked -- survives every
    sync round trip instead of being silently re-derived (and losing
    items or checked state) on each read.
    """

    text: str
    checked: bool = False
    phase: str = "implementation"

    def __post_init__(self) -> None:
        if self.phase not in {"implementation", "audit"}:
            raise ValueError(f"DoD phase must be 'implementation' or 'audit', got {self.phase!r}")

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "checked": self.checked,
            "phase": self.phase,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DoDItem":
        return cls(text=data.get("text", ""), checked=bool(data.get("checked")), phase=data.get("phase", "implementation"))


@dataclass
class GoogleDriveRef:
    """Reference to a Google Drive folder/file. A pointer only, not a
    duplicated source of truth."""

    url: str
    label: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["GoogleDriveRef"]:
        if not data:
            return None
        return cls(url=data["url"], label=data.get("label"))


@dataclass
class ProjectRecord:
    """Unified project record.

    Fields map directly onto a Trello card:
      - name                     -> card name
      - priority                 -> "P0".."P5" label, or a decimal subpriority
        such as "P2.01" within a split Inbox batch
      - status                   -> the Trello list the card belongs to
      - main_task                -> card description (structured block)
      - open_feedback             -> card description (structured block)
      - next_step                 -> card description (structured block)
      - orchestrator_ready_task    -> card description (structured block)
      - last_output                -> card description (structured block)
      - checkpoint                 -> card description (structured block)
      - blocked_by                 -> card description (structured block)
      - stop_reason / retry_after   -> card description (structured block);
        why the last run stopped and, for a provider limit, when it is
        safe to try again -- synced back after every run (see
        trello_sync.sync_project_to_trello)
      - github_repo / google_drive_ref -> card description (structured block),
        reference only, never a second source of truth
      - project_key                -> any non-priority Trello label on the
        card (see trello_sync.project_key_from_labels). This is the card's
        stable *project* identity, set once and independent of both the
        card's title text and its P0-P5 priority label -- a work card's
        title is free-form status prose ("Izolace testovacich Slack
        notifikaci") that need not mention the project it belongs to at
        all, so identity cannot be inferred from title wording alone.
    """

    name: str
    priority: int = 0
    status: ProjectStatus = ProjectStatus.NEW
    main_task: str = ""
    open_feedback: list = field(default_factory=list)
    next_step: str = ""
    orchestrator_ready_task: str = ""
    last_output: str = ""
    checkpoint: dict = field(default_factory=dict)
    blocked_by: Optional[str] = None
    stop_reason: Optional[str] = None
    retry_after: Optional[str] = None
    status_updated_at: Optional[str] = None
    waiting_since: Optional[str] = None
    completed_at: Optional[str] = None
    project_key: Optional[str] = None
    # Unattended blocked-task recovery (see recovery.py): how many
    # auto-recovery cycles have requeued this project since it last made
    # real forward progress (reset on a successful run - see
    # runner.run_once), and the earliest time the recovery scan should
    # reconsider it again while still blocked. Never set outside
    # recovery.py / runner.py.
    recovery_attempts: int = 0
    review_at: Optional[str] = None

    # Human-intervention visibility (see daemon._run_recovery_pass):
    # ``human_notified_reason`` is the exact reason text already reported to
    # Trello/operator status for the current block - comparing against it is what lets
    # the recovery pass record one visible status per distinct blocked state
    # instead of repeating it every time the backoff review comes due.
    # ``human_action_step`` is the concrete action a human should take,
    # shown alongside the reason in the visible
    # Trello card text. Both are cleared back to None the moment the block
    # is lifted (auto-recovered or fixed by a human directly on the board),
    # which records the one-time "processing resumed" transition.
    human_notified_reason: Optional[str] = None
    human_action_step: Optional[str] = None

    # The card's permalink (e.g. "https://trello.com/c/abc123"), read back
    # from Trello on every fetch (see trello_sync.project_from_card) - never
    # persisted through PM-DATA, since it is derived Trello metadata, not
    # something this process owns. Included here (rather than threaded
    # separately) so any caller that already has a ProjectRecord - operator
    # notification, visible card text - can link straight to the card.
    trello_card_url: Optional[str] = None

    # The card's full Definition-of-Done checklist -- merged, in
    # deterministic order, from the visible checklist above the PM-DATA
    # block and any explicit structured checklist (see
    # trello_sync.project_from_card) -- and each item's checked state.
    # Never re-derived once set: persisted verbatim through every sync so
    # a run can only ever add checked items, never drop or re-shuffle the
    # original set (see orchestrator_handoff.apply_dod_progress).
    dod: list = field(default_factory=list)

    github_repo: Optional[GitHubRef] = None
    google_drive_ref: Optional[GoogleDriveRef] = None

    trello_card_id: Optional[str] = None
    trello_list_id: Optional[str] = None
    provider: Optional[str] = None
    # Fields from a newer/extended but still compatible card contract that
    # this PM does not interpret. They must survive every read/write roundtrip.
    extra_data: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            self.status = ProjectStatus(self.status)
        if not (MIN_PRIORITY <= self.priority <= MAX_PRIORITY):
            raise ValueError(
                f"priority must be between {MIN_PRIORITY} and {MAX_PRIORITY}, got {self.priority}"
            )

    @property
    def is_blocked(self) -> bool:
        """Lifecycle-authoritative: the physical Trello list (``status``)
        decides, never stale ``blocked_by`` metadata on its own.

        ``status == BLOCKED`` is always blocked. ``PAUSED`` (the shared
        "Čeká na AI" list also used for plain provider-limit waits, see
        trello_sync.LIST_NAME_TO_STATUS) is only blocked when a real
        ``blocked_by`` reason is attached - this is what keeps a
        legacy-mapped blocked card (no stored ``lifecycle_status`` yet)
        eligible for recovery.py's scan. Every other status (READY,
        IN_PROGRESS, DONE, ...) is never blocked, even if ``blocked_by``
        still carries a leftover reason from before the card was moved -
        see trello_sync.project_from_card, which clears that leftover at
        load time, and scheduler.is_schedulable, which must not be
        overridden by it either.
        """
        if self.status == ProjectStatus.BLOCKED:
            return True
        if self.status == ProjectStatus.PAUSED:
            return bool(self.blocked_by)
        return False

    @property
    def returned_from_testing(self) -> bool:
        """Whether the card is corrective work returned by the audit gate."""
        return bool(self.extra_data.get("returned_from_testing"))

    def mark_returned_from_testing(self, reason: Optional[str] = None) -> None:
        """Record an audit return without losing its Trello priority context."""
        self.extra_data["returned_from_testing"] = True
        if reason:
            self.extra_data["return_reason"] = reason

    def transition_to(self, status: ProjectStatus | str, at: Optional[str] = None) -> None:
        """Change lifecycle state and maintain its human-visible timestamps."""
        target = ProjectStatus(status) if isinstance(status, str) else status
        stamp = at or datetime.now(timezone.utc).isoformat()
        if target != self.status:
            self.status_updated_at = stamp
        self.status = target
        if target == ProjectStatus.DONE and not self.completed_at:
            self.completed_at = stamp
        if target in {ProjectStatus.PAUSED, ProjectStatus.BLOCKED, ProjectStatus.ERROR}:
            if not self.waiting_since:
                self.waiting_since = stamp
        else:
            self.waiting_since = None

    def to_dict(self) -> dict:
        data: dict[str, Any] = {
            "name": self.name,
            "priority": self.priority,
            "status": self.status.value,
            "main_task": self.main_task,
            "open_feedback": list(self.open_feedback),
            "next_step": self.next_step,
            "orchestrator_ready_task": self.orchestrator_ready_task,
            "last_output": self.last_output,
            "checkpoint": dict(self.checkpoint),
            "blocked_by": self.blocked_by,
            "stop_reason": self.stop_reason,
            "retry_after": self.retry_after,
            "status_updated_at": self.status_updated_at,
            "waiting_since": self.waiting_since,
            "completed_at": self.completed_at,
            "recovery_attempts": self.recovery_attempts,
            "review_at": self.review_at,
            "human_notified_reason": self.human_notified_reason,
            "human_action_step": self.human_action_step,
            "trello_card_url": self.trello_card_url,
            "dod": [item.to_dict() for item in self.dod],
            "github_repo": self.github_repo.to_dict() if self.github_repo else None,
            "google_drive_ref": self.google_drive_ref.to_dict() if self.google_drive_ref else None,
            "trello_card_id": self.trello_card_id,
            "trello_list_id": self.trello_list_id,
            "provider": self.provider,
            "project_key": self.project_key,
            "extra_data": dict(self.extra_data),
        }
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectRecord":
        data = dict(data)
        data["github_repo"] = GitHubRef.from_dict(data.get("github_repo"))
        data["google_drive_ref"] = GoogleDriveRef.from_dict(data.get("google_drive_ref"))
        data["dod"] = [DoDItem.from_dict(item) for item in data.get("dod") or []]
        return cls(**data)
