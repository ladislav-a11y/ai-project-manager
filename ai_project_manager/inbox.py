"""Trello Inbox intake: the single manual input point.

A human's only interaction with the system is dropping a card into the
Trello "Inbox" list. This module reads those cards, classifies each one
against existing projects using a cheap local heuristic (no AI tokens
spent on routine intake), and assigns it to the matching project -
creating a new project record when nothing matches closely enough.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from .models import ProjectRecord, ProjectStatus

# Below this score a card is treated as belonging to a brand-new project
# rather than an existing one.
DEFAULT_MATCH_THRESHOLD = 0.34

_WORD_RE = re.compile(r"[a-zA-Z0-9áčďéěíňóřšťúůýž]+", re.IGNORECASE)


def _tokenize(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "") if len(w) > 2}


def _score(card_text: str, project: ProjectRecord) -> float:
    card_tokens = _tokenize(card_text)
    if not card_tokens:
        return 0.0
    project_tokens = _tokenize(project.name) | _tokenize(project.main_task)
    if not project_tokens:
        return 0.0
    overlap = card_tokens & project_tokens
    if not overlap:
        return 0.0
    return len(overlap) / min(len(card_tokens), len(project_tokens))


@dataclass
class ClassificationResult:
    card_id: str
    project_name: str
    is_new_project: bool
    confidence: float
    as_feedback: bool = False


# A classifier is any callable (card, projects) -> ClassificationResult.
# The default is a free local heuristic; a smarter (possibly
# AI-assisted) classifier can be swapped in without touching callers.
ClassifierFn = Callable[[dict, list[ProjectRecord]], ClassificationResult]


def looks_like_feedback(text: str) -> bool:
    """Heuristic: does this inbox item read as feedback/a bug on existing
    work rather than a new task?"""
    keywords = ("bug", "chyba", "nefunguje", "feedback", "oprav", "fix", "regrese", "error")
    lowered = (text or "").lower()
    return any(k in lowered for k in keywords)


def classify_inbox_card(
    card: dict,
    projects: list[ProjectRecord],
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> ClassificationResult:
    """Match an Inbox card to the best existing project, or flag it as a
    new project when nothing matches well enough. Pure, local, free."""
    text = f"{card.get('name', '')} {card.get('desc', '')}"

    best: Optional[ProjectRecord] = None
    best_score = 0.0
    for project in projects:
        score = _score(text, project)
        if score > best_score:
            best_score = score
            best = project

    if best is not None and best_score >= threshold:
        return ClassificationResult(
            card_id=card.get("id", ""),
            project_name=best.name,
            is_new_project=False,
            confidence=best_score,
            as_feedback=looks_like_feedback(text),
        )

    return ClassificationResult(
        card_id=card.get("id", ""),
        project_name=card.get("name", "").strip() or "Untitled project",
        is_new_project=True,
        confidence=best_score,
        as_feedback=False,
    )


def apply_classification(
    card: dict,
    result: ClassificationResult,
    projects_by_name: dict[str, ProjectRecord],
    default_priority: int = 2,
) -> ProjectRecord:
    """Fold a classified inbox card into the target ProjectRecord: either
    a fresh record (new project) or the existing one, with the card's
    text appended as the next step / open feedback."""
    text = card.get("desc", "").strip() or card.get("name", "").strip()

    if result.is_new_project:
        return ProjectRecord(
            name=result.project_name,
            priority=default_priority,
            status=ProjectStatus.NEW,
            main_task=text,
            next_step=text,
        )

    project = projects_by_name[result.project_name]
    if result.as_feedback:
        project.open_feedback = [*project.open_feedback, text]
    else:
        project.next_step = text
    return project


def process_inbox(
    client,
    projects: list[ProjectRecord],
    classifier: ClassifierFn = classify_inbox_card,
    inbox_list_name: str = "Inbox",
    default_priority: int = 2,
) -> list[ProjectRecord]:
    """Fetch new cards from the Trello Inbox list, classify each one and
    fold it into the right project. Returns the list of ProjectRecords
    that changed (new ones included) so the caller can sync them back.

    Processed inbox cards are archived-in-place by moving them out of
    Inbox onto their target project's list by the caller after sync;
    this function only performs classification/merging.
    """
    from .trello_sync import build_list_maps

    id_to_name, name_to_id = build_list_maps(client)
    inbox_list_id = name_to_id.get(inbox_list_name)
    if inbox_list_id is None:
        return []

    projects_by_name = {p.name: p for p in projects}
    changed: list[ProjectRecord] = []

    for card in client.list_cards(inbox_list_id):
        result = classifier(card, list(projects_by_name.values()))
        project = apply_classification(card, result, projects_by_name, default_priority=default_priority)
        projects_by_name[project.name] = project
        changed.append(project)

    return changed
