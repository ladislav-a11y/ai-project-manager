"""Trello Inbox intake: the single manual input point.

A human's only interaction with the system is dropping a card into the
Trello "Inbox" list. Production preparation is performed by the separate
AI planner and is validated here before admission. The deterministic splitter
remains for tests and backwards-compatible callers only.
"""

from __future__ import annotations

import re
import hashlib
import logging
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, MutableMapping, Optional

from .models import DoDItem, ProjectRecord, ProjectStatus
from .inbox_preparation import (
    PreparedTask,
    build_dod,
    inbox_source_text,
    prepare_inbox_card,
    prioritize_inbox_cards,
    visible_inbox_description,
    task_execution_order,
)

logger = logging.getLogger("ai_project_manager")

# Inbox preparation is a planning/classification phase, not worker work.
# Keep this policy executable even if an AI planner is added later: Hermes is
# deliberately reserved for already-prepared atomic cards. Gemini is retired
# from PM entirely after its migration/audit outcome.
INBOX_PLANNING_FORBIDDEN_PROVIDERS = frozenset({"hermes", "gemini"})


def inbox_planner_providers(providers) -> tuple[str, ...]:
    """Return providers allowed to plan Inbox input, never including Hermes."""
    return tuple(
        provider for provider in providers
        if str(provider).strip().casefold() not in INBOX_PLANNING_FORBIDDEN_PROVIDERS
    )

# Below this score a card is treated as belonging to a brand-new project
# rather than an existing one.
DEFAULT_MATCH_THRESHOLD = 0.34

# Legacy acknowledgement used by older PM versions. Current code moves every
# successfully processed Inbox card into the proper workflow instead of
# leaving acknowledged cards behind.
PROCESSED_MARKER = "<!-- PM-INBOX-PROCESSED -->"
INBOX_RECEIPTS_KEY = "inbox_receipts"
INBOX_SOURCE_KEY = "inbox_source"

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
PersistProjectFn = Callable[[ProjectRecord], Optional[Mapping]]
InboxPlannerFn = Callable[[dict, list[ProjectRecord]], Optional[dict]]


@dataclass(frozen=True)
class InboxReceiptMatch:
    """An existing board record proving that an Inbox card was handled."""

    project: ProjectRecord
    receipt: dict
    matched_by: str


def _normalize_source_text(text: str) -> str:
    """Normalize editable Inbox text before computing its stable fingerprint."""
    normalized = unicodedata.normalize("NFKC", text or "")
    return " ".join(normalized.casefold().split())


def inbox_content_hash(card: dict) -> str:
    """Return a deterministic, non-secret fingerprint of an Inbox card."""
    source_text = "\n".join(
        (
            _normalize_source_text(str(card.get("name") or "")),
            _normalize_source_text(visible_inbox_description(card)),
        )
    )
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()


def inbox_source_reference(card: dict, target_card_id: Optional[str] = None) -> dict:
    """Build the source receipt persisted on the main-board target card.

    The main Trello Inbox is writable by PM, while the separate personal
    Inbox is not part of this client.  The source card ID remains primary;
    URL and content hash are secondary duplicate/revision evidence.
    """
    source_id = str(card.get("id") or "").strip()
    if not source_id:
        raise ValueError("Inbox card must have a non-empty id")
    reference = {
        "source_system": "trello_inbox",
        "source_card_id": source_id,
        "content_sha256": inbox_content_hash(card),
    }
    source_url = str(card.get("url") or "").strip()
    if source_url:
        reference["source_card_url"] = source_url
    if target_card_id:
        reference["target_card_id"] = str(target_card_id)
    return reference


def _project_inbox_receipts(project: ProjectRecord):
    extra = project.extra_data or {}
    direct = extra.get(INBOX_SOURCE_KEY)
    if isinstance(direct, dict):
        yield direct
    receipts = extra.get(INBOX_RECEIPTS_KEY)
    if isinstance(receipts, list):
        for receipt in receipts:
            if isinstance(receipt, dict):
                yield receipt
    # Keep legacy IDs useful for cards written by older PM versions, even
    # though those records do not carry a content hash yet.
    legacy_ids = extra.get("processed_inbox_card_ids")
    if isinstance(legacy_ids, list):
        for source_id in legacy_ids:
            if isinstance(source_id, str) and source_id:
                yield {"source_card_id": source_id}


def find_inbox_receipt(
    projects: list[ProjectRecord], card: dict
) -> Optional[InboxReceiptMatch]:
    """Find an exact source-ID, URL, or content-hash receipt on the board.

    A content-hash match is deliberately treated as a duplicate only when
    it is exact.  Fuzzy/semantic similarity belongs to a later review step
    and must never silently merge two different Inbox requests.
    """
    reference = inbox_source_reference(card)
    source_id = reference["source_card_id"]
    source_url = reference.get("source_card_url")
    content_hash = reference["content_sha256"]
    revision_match: Optional[InboxReceiptMatch] = None
    for project in projects:
        for receipt in _project_inbox_receipts(project):
            if receipt.get("source_card_id") == source_id:
                if receipt.get("content_sha256") == content_hash:
                    return InboxReceiptMatch(project, receipt, "source_card_id")
                revision_match = InboxReceiptMatch(project, receipt, "source_card_id_revision")
                continue
            if source_url and receipt.get("source_card_url") == source_url:
                if receipt.get("content_sha256") == content_hash:
                    return InboxReceiptMatch(project, receipt, "source_card_url")
                revision_match = InboxReceiptMatch(project, receipt, "source_card_url_revision")
                continue
            if receipt.get("content_sha256") == content_hash:
                return InboxReceiptMatch(project, receipt, "content_sha256")
    return revision_match


def record_inbox_receipt(
    project: ProjectRecord,
    card: dict,
    *,
    target_card_id: Optional[str] = None,
) -> dict:
    """Persist one idempotent Inbox receipt in the target card contract."""
    reference = inbox_source_reference(card, target_card_id=target_card_id)
    extra = project.extra_data if isinstance(project.extra_data, dict) else {}
    receipts = extra.get(INBOX_RECEIPTS_KEY)
    if not isinstance(receipts, list):
        receipts = []
    if not any(
        isinstance(existing, dict)
        and (
            (
                existing.get("source_card_id") == reference["source_card_id"]
                and existing.get("content_sha256") == reference["content_sha256"]
            )
            or (
                reference.get("source_card_url")
                and existing.get("source_card_url") == reference["source_card_url"]
                and existing.get("content_sha256") == reference["content_sha256"]
            )
            or existing.get("content_sha256") == reference["content_sha256"]
        )
        for existing in receipts
    ):
        receipts.append(reference)
    else:
        # A newly-created split child may only learn its Trello ID after the
        # first sync. Bind that ID to the existing source receipt on the
        # follow-up write instead of creating another receipt.
        for existing in receipts:
            if (
                isinstance(existing, dict)
                and existing.get("source_card_id") == reference["source_card_id"]
                and existing.get("content_sha256") == reference["content_sha256"]
                and target_card_id
            ):
                existing["target_card_id"] = str(target_card_id)
    extra[INBOX_RECEIPTS_KEY] = receipts
    # Preserve the old field for backwards-compatible readback and tests.
    legacy_ids = extra.get("processed_inbox_card_ids")
    if not isinstance(legacy_ids, list):
        legacy_ids = []
    if reference["source_card_id"] not in legacy_ids:
        legacy_ids.append(reference["source_card_id"])
    extra["processed_inbox_card_ids"] = legacy_ids
    project.extra_data = extra
    return reference


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
    text = f"{card.get('name', '')} {visible_inbox_description(card)}"

    best: Optional[ProjectRecord] = None
    best_score = 0.0
    # A terminal project is not a valid destination for new Inbox work.  A
    # keyword overlap with a historical Hotovo card must create a fresh
    # Ready item instead of silently turning unfinished input into a Done
    # receipt.
    for project in projects:
        if project.status == ProjectStatus.DONE:
            continue
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
    text = inbox_source_text(card)

    if result.is_new_project:
        return ProjectRecord(
            name=result.project_name,
            priority=default_priority,
            status=ProjectStatus.NEW,
            main_task=text,
            next_step=text,
        )

    project = projects_by_name[result.project_name]
    receipt_ids = list((project.extra_data or {}).get("processed_inbox_card_ids") or [])
    card_id = card.get("id")
    if card_id in receipt_ids:
        return project
    if result.as_feedback:
        project.open_feedback = [*project.open_feedback, text]
    else:
        project.next_step = text
    record_inbox_receipt(project, card, target_card_id=project.trello_card_id)
    return project


def _is_processed(card: dict) -> bool:
    return PROCESSED_MARKER in (card.get("desc") or "")


def _build_inbox_receipt_card(
    card: dict,
    target: ProjectRecord,
    *,
    outcome: str,
    matched_by: Optional[str] = None,
):
    """Represent an Inbox source card as a durable Done receipt.

    This is safe because the main board Inbox is PM-owned.  It is not used
    for the separate personal read-only Inbox.
    """
    source = inbox_source_reference(card, target_card_id=target.trello_card_id)
    details = f"Zařazeno do karty: {target.name}"
    if outcome == "duplicate":
        details = f"Duplicitní požadavek; již pokryto kartou: {target.name}"
    elif outcome == "revision":
        details = f"Nová revize požadavku; zpracováno v kartě: {target.name}"
    if matched_by:
        details += f" (shoda podle {matched_by})"
    receipt = ProjectRecord(
        name=f"Zpracováno — {card.get('name', '').strip() or 'Inbox položka'}",
        priority=target.priority,
        status=ProjectStatus.DONE,
        main_task=(card.get("desc") or card.get("name") or "").strip(),
        last_output=details,
        dod=[DoDItem(text="Položka Inboxu byla zpracována a zařazena.", checked=True)],
        trello_card_id=card.get("id"),
        trello_card_url=card.get("url"),
        trello_list_id=card.get("list_id"),
        extra_data={
            "inbox_receipt": source,
            "inbox_receipt_outcome": outcome,
            "inbox_target_card_id": target.trello_card_id,
        },
    )
    receipt.transition_to(ProjectStatus.DONE)
    return receipt


def process_inbox(
    client,
    projects: list[ProjectRecord],
    classifier: ClassifierFn = classify_inbox_card,
    inbox_list_name: str = "Inbox",
    default_priority: int = 2,
    persist_project: Optional[PersistProjectFn] = None,
    project_paths: Optional[MutableMapping[str, str]] = None,
    card_project_keys: Optional[Mapping[str, str]] = None,
    projects_root: Optional[str] = None,
    planner: Optional[InboxPlannerFn] = None,
) -> list[ProjectRecord]:
    """Fetch new (not yet processed) cards from the Trello Inbox list,
    classify each one and fold it into the right project. Returns the
    list of ProjectRecords that changed (new ones included) so the
    caller can sync them back.

    New work keeps the original card identity and moves to Ready/New. Input
    folded into an existing project becomes a completed receipt card. The
    target contract stores the Inbox card ID, making retries idempotent if a
    write fails between persisting the target and moving the receipt.
    """
    from .trello_sync import build_list_maps

    id_to_name, name_to_id = build_list_maps(client)
    inbox_list_id = name_to_id.get(inbox_list_name)
    if inbox_list_id is None:
        return []

    projects_by_name = {p.name: p for p in projects}
    changed: list[ProjectRecord] = []
    cards = client.list_cards(inbox_list_id)
    batch_priorities = prioritize_inbox_cards(cards, default_priority=default_priority)

    # Intake is deliberately one source project per tick.  This keeps the
    # AI planning boundary small and observable, and prevents a long Inbox
    # batch from making another project look selected in the same tick.  The
    # highest-priority source card is the only one admitted; a failed-closed
    # identity therefore also blocks lower-priority cards until the next
    # controlled tick instead of silently bypassing the issue.
    if cards:
        cards = [
            min(
                cards,
                key=lambda card: (
                    -batch_priorities.get(str(card.get("id") or ""), (default_priority, ""))[0],
                    str(card.get("id") or ""),
                ),
            )
        ]

    for card in cards:
        if _is_processed(card) and persist_project is None:
            continue

        # Check the immutable source ID first, then URL/content hash.  This
        # makes retries and a second copy of the same Inbox request safe
        # without relying on an editable title or a local database.
        source_id = str(card.get("id") or "")
        partial_split = {
            int(preparation["subtask_index"]): project
            for project in projects_by_name.values()
            for preparation in [
                (project.extra_data or {}).get("inbox_preparation", {})
            ]
            if (
                isinstance(preparation, dict)
                and preparation.get("source_card_id") == source_id
                and isinstance(preparation.get("subtask_index"), int)
            )
        }
        # A split preparation marker is not a reason to skip receipt lookup.
        # The source card may have remained in Inbox after a partial write, or
        # its content may now be a revision; both cases must first reconcile
        # against the durable receipt before preparing more children.
        previous = find_inbox_receipt(list(projects_by_name.values()), card)
        if partial_split and previous is not None and previous.matched_by == "source_card_id":
            expected_split_counts = [
                int((project.extra_data or {}).get("inbox_preparation", {}).get("subtask_count", 0))
                for project in partial_split.values()
                if isinstance((project.extra_data or {}).get("inbox_preparation"), dict)
            ]
            expected_split_count = max(expected_split_counts, default=0)
            canonical_source = any(
                preparation.get("subtask_index") == 0
                and project.trello_card_id == source_id
                for project in projects_by_name.values()
                for preparation in [(project.extra_data or {}).get("inbox_preparation", {})]
                if isinstance(preparation, dict)
            )
            if not canonical_source or (
                expected_split_count and len(partial_split) < expected_split_count
            ):
                # An exact receipt belonging to an already-persisted split
                # child does not mean the source request is fully handled.
                # Continue the split preparation so the missing children are
                # created, stale child metadata is reconciled, and the source
                # card remains the canonical target.
                previous = None
        if previous is not None:
            target = previous.project
            if previous.matched_by.endswith("_revision"):
                target_card_id = previous.receipt.get("target_card_id") or target.trello_card_id
                target = next(
                    (
                        candidate
                        for candidate in projects_by_name.values()
                        if candidate.trello_card_id == target_card_id
                    ),
                    target,
                )
                text = card.get("desc", "").strip() or card.get("name", "").strip()
                if looks_like_feedback(text):
                    target.open_feedback = [*target.open_feedback, text]
                else:
                    target.next_step = text
                record_inbox_receipt(target, card, target_card_id=target.trello_card_id)
                if persist_project is not None:
                    persist_project(target)
                    if target.trello_card_id == card.get("id"):
                        # A new revision of a source card that is also the
                        # target work card must remain the work card; do not
                        # turn it into a Done receipt.
                        continue
                    from .trello_sync import sync_project_to_trello
                    sync_project_to_trello(
                        client,
                        _build_inbox_receipt_card(
                            card,
                            target,
                            outcome="revision",
                            matched_by=previous.matched_by,
                        ),
                    )
                continue
            if target.trello_card_id == card.get("id"):
                # A prior write may have persisted the target contract but
                # failed before moving the original Inbox card.  Re-syncing
                # that same card completes the intended board transition.
                if persist_project is not None:
                    persist_project(target)
                continue
            if persist_project is not None:
                from .trello_sync import sync_project_to_trello
                sync_project_to_trello(
                    client,
                    _build_inbox_receipt_card(
                        card,
                        target,
                        outcome="duplicate",
                        matched_by=previous.matched_by,
                    ),
                )
            continue

        result = classifier(card, list(projects_by_name.values()))
        planner_result = None
        planned_tasks = None
        if planner is not None:
            planner_result = planner(card, list(projects_by_name.values()))
            if not planner_result or not planner_result.get("tasks"):
                logger.warning(
                    "Inbox card left in Inbox: AI planner produced no valid plan id=%s name=%r",
                    card.get("id"), card.get("name"),
                )
                continue
            planned_tasks = tuple(planner_result["tasks"])
        if planner is not None or result.is_new_project or partial_split:
            preparation_card = card
            if partial_split:
                # A source card may have been manually restored to Inbox and
                # lose its PM-DATA/identity label while its already-persisted
                # children still carry the durable source binding. Recover
                # the identity only when every known child agrees; never
                # infer it from fuzzy title similarity.
                child_identities = {
                    project.project_key
                    for project in partial_split.values()
                    if project.project_key
                }
                source_identities = {
                    str(label.get("name") if isinstance(label, dict) else label).strip()
                    for label in card.get("labels", []) or []
                    if str(label.get("name") if isinstance(label, dict) else label).strip()
                    and not re.match(
                        r"^P[0-5](?:\.\d+)?$",
                        str(label.get("name") if isinstance(label, dict) else label).strip(),
                        flags=re.IGNORECASE,
                    )
                }
                if not source_identities and len(child_identities) == 1:
                    preparation_card = dict(card)
                    preparation_card["labels"] = [{"name": next(iter(child_identities))}]
            preparation = prepare_inbox_card(
                preparation_card,
                project_paths=project_paths,
                card_project_keys=card_project_keys,
                default_priority=default_priority,
                priority_override=batch_priorities.get(str(card.get("id") or "")),
                projects_root=projects_root,
                allow_new_project=result.is_new_project,
                planned_tasks=planned_tasks,
            )
            if preparation.human_required_reason:
                logger.warning(
                    "Inbox card requires human project assignment id=%s name=%r reason=%s",
                    card.get("id"), card.get("name"), preparation.human_required_reason,
                )
                continue

            source_reference = inbox_source_reference(card)
            if preparation.generated_project and preparation.project_key and preparation.project_path:
                if project_paths is not None:
                    existing_path = project_paths.get(preparation.project_key)
                    if existing_path and str(Path(existing_path).resolve()) != str(Path(preparation.project_path).resolve()):
                        logger.warning(
                            "Inbox card requires human project assignment id=%s name=%r reason=generated project identity conflicts with configured path",
                            card.get("id"), card.get("name"),
                        )
                        continue
                    project_paths[preparation.project_key] = preparation.project_path
                Path(preparation.project_path).mkdir(parents=True, exist_ok=True)
            # Persist split children before mutating/moving the source card.
            # Thus any failure leaves the canonical source in Inbox, while a
            # retry can recognize already durable children by source/index.
            execution_order = task_execution_order(preparation.tasks)
            ordered_tasks = [
                (index, preparation.tasks[index]) for index in execution_order
            ]
            prepared_projects: dict[int, ProjectRecord] = dict(partial_split)
            for index, prepared_task in ordered_tasks:
                if index in partial_split:
                    # A prior attempt may have persisted only part of the
                    # split, or may have used an older priority rubric. Keep
                    # the durable card identity but reconcile its task text,
                    # DoD, metadata, and priority before continuing.
                    task = partial_split[index]
                    task.name = prepared_task.title
                    task.priority = prepared_task.priority
                    task.main_task = prepared_task.task
                    task.next_step = prepared_task.next_step
                    task.orchestrator_ready_task = (
                        f"Implementovat tento samostatný rozsah v projektu "
                        f"{task.project_key or prepared_task.title}: {prepared_task.task} "
                        "Zachovat chování mimo tento rozsah."
                    )
                    task.dod = list(build_dod((prepared_task,)))
                    metadata = task.extra_data.setdefault("inbox_preparation", {})
                    metadata.update({
                        "subtask_count": len(preparation.tasks),
                        "scope": prepared_task.scope,
                        "source_priority": preparation.priority,
                        "source_priority_reason": preparation.priority_reason,
                        "task_priority": prepared_task.priority,
                        "priority_reason": prepared_task.priority_reason,
                        "depends_on_subtask_indices": list(prepared_task.depends_on),
                        "execution_order": execution_order.index(index),
                        "dod": [item.to_dict() for item in task.dod],
                        "project_path": preparation.project_path,
                            "generated_project": preparation.generated_project,
                            "intake_provider": (planner_result or {}).get("provider"),
                            "intake_model": (planner_result or {}).get("model"),
                            "intake_provider_reason": (planner_result or {}).get("provider_reason"),
                            "intake_model_reason": (planner_result or {}).get("model_reason"),
                            "intake_selection_reason": (planner_result or {}).get("selection_reason"),
                        })
                    record_inbox_receipt(task, card, target_card_id=task.trello_card_id)
                    if persist_project is not None:
                        persist_project(task)
                    continue
                task = ProjectRecord(
                    name=prepared_task.title,
                    priority=prepared_task.priority,
                    status=ProjectStatus.NEW,
                    main_task=prepared_task.task,
                    next_step=prepared_task.next_step,
                    orchestrator_ready_task=(
                        f"Implementovat tento samostatný rozsah v projektu "
                        f"{prepared_task.project_key or prepared_task.title}: {prepared_task.task} "
                        "Zachovat chování mimo tento rozsah."
                    ),
                    dod=list(build_dod((prepared_task,))),
                    # Each subtask is routed by its own resolved identity
                    # (see ``inbox_preparation._task_project_key``), not by
                    # blindly inheriting the source card's identity.
                    project_key=prepared_task.project_key,
                    extra_data={
                        "inbox_preparation": {
                            "source_card_id": source_reference["source_card_id"],
                            "source_card_url": source_reference.get("source_card_url"),
                            "content_sha256": source_reference["content_sha256"],
                            "subtask_index": index,
                            "subtask_count": len(preparation.tasks),
                            "scope": prepared_task.scope,
                            "source_priority": preparation.priority,
                            "source_priority_reason": preparation.priority_reason,
                            "task_priority": prepared_task.priority,
                            "priority_reason": prepared_task.priority_reason,
                            "depends_on_subtask_indices": list(prepared_task.depends_on),
                            "execution_order": execution_order.index(index),
                            "dod": [item.to_dict() for item in build_dod((prepared_task,))],
                            "project_path": preparation.project_path,
                            "generated_project": preparation.generated_project,
                            "intake_provider": (planner_result or {}).get("provider"),
                            "intake_model": (planner_result or {}).get("model"),
                            "intake_provider_reason": (planner_result or {}).get("provider_reason"),
                            "intake_model_reason": (planner_result or {}).get("model_reason"),
                            "intake_selection_reason": (planner_result or {}).get("selection_reason"),
                        }
                    },
                )
                if index == 0:
                    # Preserve the source card as the first canonical target.
                    task.trello_card_id = card.get("id")
                    task.trello_card_url = card.get("url")
                    task.trello_list_id = card.get("list_id")
                record_inbox_receipt(task, card, target_card_id=task.trello_card_id)
                projects_by_name[task.name] = task
                if persist_project is not None:
                    persisted = persist_project(task)
                    # Adapters may return the created Trello card instead of
                    # mutating the ProjectRecord. Bind that identity here so
                    # split children remain idempotent on retry.
                    if isinstance(persisted, Mapping) and not task.trello_card_id:
                        task.trello_card_id = persisted.get("id")
                        task.trello_card_url = persisted.get("url", task.trello_card_url)
                        task.trello_list_id = persisted.get("list_id", task.trello_list_id)
                    if index > 0:
                        # The first sync creates the child card and the second
                        # sync binds its generated identity into PM-DATA.
                        record_inbox_receipt(task, card, target_card_id=task.trello_card_id)
                        persist_project(task)
                prepared_projects[index] = task
            changed.extend(
                prepared_projects[index]
                for index in range(len(preparation.tasks))
                if index in prepared_projects
            )
            continue

        project = apply_classification(card, result, projects_by_name, default_priority=default_priority)
        projects_by_name[project.name] = project

        if result.is_new_project:
            # Move this exact card through the workflow. Never create a
            # duplicate with a new Trello identity for the same Inbox item.
            project.trello_card_id = card.get("id")
            project.trello_card_url = card.get("url")
            project.trello_list_id = card.get("list_id")
            record_inbox_receipt(project, card, target_card_id=project.trello_card_id)

        # This ordering is intentional. The marker is an acknowledgement,
        # not merely decoration: writing it before the project would turn a
        # transient failure of the latter write into permanent input loss.
        if persist_project is not None:
            persist_project(project)
            if not result.is_new_project:
                # The source item is itself processed work. Preserve it as a
                # durable, identity-bound receipt in Done/Hotovo.
                from .trello_sync import sync_project_to_trello
                sync_project_to_trello(
                    client,
                    _build_inbox_receipt_card(card, project, outcome="processed"),
                )
        changed.append(project)

    return changed
