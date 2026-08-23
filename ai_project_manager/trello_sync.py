"""Mapping between a Trello card and a ProjectRecord, in both directions.

Trello is the single source of truth. A ProjectRecord is always
*derived* from a card (``project_from_card``) and any change is always
*written back* onto the same card (``card_updates_from_project``) -
nothing about a project is kept anywhere else long-term.

Card <-> field mapping:
  - card name        <-> ProjectRecord.name
  - "P0".."P5" label  <-> ProjectRecord.priority
  - the list the card is in <-> ProjectRecord.status
  - a structured block inside the card description <-> everything else
    (main_task, open_feedback, next_step, orchestrator_ready_task,
    last_output, checkpoint, blocked_by, github_repo, google_drive_ref)

The structured block keeps the description human-readable: free-form
notes can live above/below the block, the block itself is fenced so it
round-trips exactly.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .models import GitHubRef, GoogleDriveRef, ProjectRecord, ProjectStatus

BLOCK_START = "<!-- PM-DATA"
BLOCK_END = "-->"
BLOCK_RE = re.compile(
    re.escape(BLOCK_START) + r"\s*(.*?)\s*" + re.escape(BLOCK_END), re.DOTALL
)

PRIORITY_LABEL_RE = re.compile(r"^P([0-5])$")

# Trello list name -> ProjectStatus. Board setup is expected to use
# exactly these list names; unknown lists fall back to ProjectStatus.NEW.
LIST_NAME_TO_STATUS = {
    "Inbox": ProjectStatus.INBOX,
    "New": ProjectStatus.NEW,
    "Ready": ProjectStatus.READY,
    "In Progress": ProjectStatus.IN_PROGRESS,
    "Paused": ProjectStatus.PAUSED,
    "Blocked": ProjectStatus.BLOCKED,
    "Done": ProjectStatus.DONE,
    "Error": ProjectStatus.ERROR,
}
STATUS_TO_LIST_NAME = {v: k for k, v in LIST_NAME_TO_STATUS.items()}


def priority_from_labels(labels: list[dict]) -> int:
    for label in labels or []:
        m = PRIORITY_LABEL_RE.match((label.get("name") or "").strip())
        if m:
            return int(m.group(1))
    return 0


def priority_label_name(priority: int) -> str:
    return f"P{priority}"


def status_from_list(list_id: Optional[str], list_id_to_name: dict[str, str]) -> ProjectStatus:
    name = list_id_to_name.get(list_id)
    return LIST_NAME_TO_STATUS.get(name, ProjectStatus.NEW)


def _parse_data_block(desc: str) -> dict:
    m = BLOCK_RE.search(desc or "")
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except (ValueError, json.JSONDecodeError):
        return {}


def _render_data_block(data: dict) -> str:
    body = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
    return f"{BLOCK_START}\n{body}\n{BLOCK_END}"


def _strip_data_block(desc: str) -> str:
    return BLOCK_RE.sub("", desc or "").strip()


def project_from_card(card: dict, list_id_to_name: dict[str, str]) -> ProjectRecord:
    """Build a ProjectRecord snapshot from a raw Trello card dict."""
    raw_desc = card.get("desc", "")
    data = _parse_data_block(raw_desc)
    notes = _strip_data_block(raw_desc)

    if not any([
        data.get("main_task"),
        data.get("orchestrator_ready_task"),
        data.get("next_step"),
        data.get("open_feedback"),
    ]) and notes:
        data["main_task"] = notes
        data["orchestrator_ready_task"] = notes
    priority = priority_from_labels(card.get("labels", []))
    status = status_from_list(card.get("list_id"), list_id_to_name)

    github_repo = GitHubRef.from_dict(data.get("github_repo"))
    google_drive_ref = GoogleDriveRef.from_dict(data.get("google_drive_ref"))

    return ProjectRecord(
        name=card.get("name", ""),
        priority=priority,
        status=status,
        main_task=data.get("main_task", ""),
        open_feedback=list(data.get("open_feedback", [])),
        next_step=data.get("next_step", ""),
        orchestrator_ready_task=data.get("orchestrator_ready_task", ""),
        last_output=data.get("last_output", ""),
        checkpoint=dict(data.get("checkpoint", {})),
        blocked_by=data.get("blocked_by"),
        stop_reason=data.get("stop_reason"),
        retry_after=data.get("retry_after"),
        github_repo=github_repo,
        google_drive_ref=google_drive_ref,
        trello_card_id=card.get("id"),
        trello_list_id=card.get("list_id"),
        provider=data.get("provider"),
    )


def card_updates_from_project(project: ProjectRecord, list_name_to_id: dict[str, str], notes: str = "") -> dict:
    """Build the ``update_card``/``create_card`` kwargs that write a
    ProjectRecord back onto its Trello card.

    ``notes`` is preserved free text that stays visible above the
    machine-readable block (e.g. a human-friendly summary).
    """
    data = {
        "main_task": project.main_task,
        "open_feedback": list(project.open_feedback),
        "next_step": project.next_step,
        "orchestrator_ready_task": project.orchestrator_ready_task,
        "last_output": project.last_output,
        "checkpoint": dict(project.checkpoint),
        "blocked_by": project.blocked_by,
        "stop_reason": project.stop_reason,
        "retry_after": project.retry_after,
        "github_repo": project.github_repo.to_dict() if project.github_repo else None,
        "google_drive_ref": project.google_drive_ref.to_dict() if project.google_drive_ref else None,
        "provider": project.provider,
    }
    desc_parts = [notes.strip()] if notes.strip() else []
    desc_parts.append(_render_data_block(data))
    desc = "\n\n".join(desc_parts)

    list_name = STATUS_TO_LIST_NAME.get(project.status, "New")
    list_id = list_name_to_id.get(list_name)

    return {
        "name": project.name,
        "desc": desc,
        "list_id": list_id,
        "labels": [priority_label_name(project.priority)],
    }


def build_list_maps(client) -> tuple[dict[str, str], dict[str, str]]:
    """Return (list_id -> name, name -> list_id) for the client's board."""
    lists = client.list_lists()
    id_to_name = {lst["id"]: lst["name"] for lst in lists}
    name_to_id = {lst["name"]: lst["id"] for lst in lists}
    return id_to_name, name_to_id


def fetch_all_projects(client, exclude_list_names: tuple[str, ...] = ("Inbox",)) -> list[ProjectRecord]:
    """Fetch every project card from the board (excluding Inbox, which
    holds unclassified raw input rather than assigned projects)."""
    id_to_name, _ = build_list_maps(client)
    projects: list[ProjectRecord] = []
    for list_id, name in id_to_name.items():
        if name in exclude_list_names:
            continue
        for card in client.list_cards(list_id):
            projects.append(project_from_card(card, id_to_name))
    return projects


def sync_project_to_trello(client, project: ProjectRecord, notes: str = "") -> dict:
    """Push a ProjectRecord's current state back onto its Trello card.

    This is the single write path back to the source of truth: status,
    priority, checkpoint, last output, next step, feedback, blocked_by
    and provider all get persisted onto the card in one update.
    """
    _, name_to_id = build_list_maps(client)
    updates = card_updates_from_project(project, name_to_id, notes=notes)
    if project.trello_card_id:
        return client.update_card(
            project.trello_card_id,
            name=updates["name"],
            desc=updates["desc"],
            list_id=updates["list_id"],
            labels=updates["labels"],
        )
    return client.create_card(
        updates["list_id"],
        updates["name"],
        desc=updates["desc"],
        labels=updates["labels"],
    )
