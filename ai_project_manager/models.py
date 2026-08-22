"""The unified project record.

Trello is the single source of truth for a project's state, priority,
task, feedback, next step and last result (see trello_sync.py for the
mapping between a Trello card and a ProjectRecord). ProjectRecord is a
plain, serializable snapshot derived from that source of truth -- it is
never an independent store that could drift from Trello.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


MIN_PRIORITY = 0
MAX_PRIORITY = 5


class ProjectStatus(str, Enum):
    """Project lifecycle state. Backed 1:1 by the Trello list the card sits in."""

    INBOX = "inbox"
    NEW = "new"
    READY = "ready"
    IN_PROGRESS = "in_progress"
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
      - priority                 -> "P0".."P5" label on the card
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

    github_repo: Optional[GitHubRef] = None
    google_drive_ref: Optional[GoogleDriveRef] = None

    trello_card_id: Optional[str] = None
    trello_list_id: Optional[str] = None
    provider: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            self.status = ProjectStatus(self.status)
        if not (MIN_PRIORITY <= self.priority <= MAX_PRIORITY):
            raise ValueError(
                f"priority must be between {MIN_PRIORITY} and {MAX_PRIORITY}, got {self.priority}"
            )

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked_by) or self.status == ProjectStatus.BLOCKED

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
            "github_repo": self.github_repo.to_dict() if self.github_repo else None,
            "google_drive_ref": self.google_drive_ref.to_dict() if self.google_drive_ref else None,
            "trello_card_id": self.trello_card_id,
            "trello_list_id": self.trello_list_id,
            "provider": self.provider,
        }
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectRecord":
        data = dict(data)
        data["github_repo"] = GitHubRef.from_dict(data.get("github_repo"))
        data["google_drive_ref"] = GoogleDriveRef.from_dict(data.get("google_drive_ref"))
        return cls(**data)
