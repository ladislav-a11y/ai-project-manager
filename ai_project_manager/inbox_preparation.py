"""Deterministic preparation of raw Trello Inbox work.

The preparation step runs before a card is admitted to the governed workflow.
It deliberately uses no provider tokens: a bounded, explainable policy is
safer for unattended intake than silently inventing a project, priority, or
Definition of Done from an opaque guess.  The result is structured so an AI
planner can replace the policy later without changing the Trello lifecycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Mapping, Optional

from .models import DoDItem


_WORD_RE = re.compile(r"[a-zA-Z0-9áčďéěíňóřšťúůýž]+", re.IGNORECASE)
_EXPLICIT_PRIORITY_RE = re.compile(r"^P([0-5])$", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\"(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ])")
_PM_DATA_BLOCK_RE = re.compile(r"<!--\s*PM-DATA.*?-->", re.DOTALL)


@dataclass(frozen=True)
class PreparedTask:
    """One independently dispatchable task derived from one Inbox card."""

    title: str
    task: str
    next_step: str
    scope: str
    priority: int = 2
    priority_reason: str = "výchozí priorita bez silnějšího signálu"


@dataclass(frozen=True)
class InboxPreparation:
    """Validated, explainable preparation result for one raw Inbox card."""

    source_name: str
    normalized_text: str
    project_key: Optional[str]
    priority: int
    priority_reason: str
    tasks: tuple[PreparedTask, ...]
    dod: tuple[DoDItem, ...]
    human_required_reason: Optional[str] = None


def visible_inbox_description(card: Mapping) -> str:
    """Return only user-authored Inbox text, excluding the machine contract."""
    return _PM_DATA_BLOCK_RE.sub("", str(card.get("desc") or "")).strip()


def inbox_source_text(card: Mapping) -> str:
    """Return the human Inbox request, falling back to its title."""
    return visible_inbox_description(card) or str(card.get("name") or "").strip()


def prioritize_inbox_cards(
    cards: list[Mapping],
    *,
    default_priority: int = 2,
) -> dict[str, tuple[int, str]]:
    """Rank a batch of Inbox cards without treating list order as priority.

    Explicit P-labels always win.  Unlabelled cards are evaluated together so
    a batch containing a live regression, a dependency, and a future idea is
    ordered consistently even when Trello returns them in a different order.
    """
    ranked: list[tuple[int, str, str]] = []
    for card in cards:
        card_id = str(card.get("id") or "")
        text = normalize_inbox_text(
            f"{card.get('name', '')} {visible_inbox_description(card)}"
        )
        priority, reason = derive_priority(card, text, default_priority)
        ranked.append((priority, card_id, reason))
    # Stable tie-breaking is deterministic and independent of Trello card
    # position; explicit priorities remain untouched.
    ranked.sort(key=lambda item: (-item[0], item[1]))
    result: dict[str, tuple[int, str]] = {}
    for index, (priority, card_id, reason) in enumerate(ranked):
        if not card_id:
            continue
        result[card_id] = (priority, f"{reason}; dávkové pořadí {index + 1}/{len(ranked)}")
    return result


def normalize_inbox_text(text: str) -> str:
    """Turn pasted literal newlines and blank paragraphs into plain prose."""
    value = str(text or "").replace("\\r\\n", " ").replace("\\n", " ")
    value = re.sub(r"\r?\n\r?\n+", ". ", value)
    value = re.sub(r"\r?\n", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = value.lstrip("¨\ufeff ")
    return value


def _tokens(value: str) -> set[str]:
    return {word.casefold() for word in _WORD_RE.findall(value or "") if len(word) > 2}


def _explicit_priority(card: Mapping) -> Optional[int]:
    for label in card.get("labels", []) or []:
        name = label.get("name") if isinstance(label, dict) else label
        match = _EXPLICIT_PRIORITY_RE.match(str(name or "").strip())
        if match:
            return int(match.group(1))
    return None


def derive_priority(card: Mapping, text: str, default_priority: int = 2) -> tuple[int, str]:
    """Derive P5..P0 from explicit labels or a documented urgency rubric."""
    explicit = _explicit_priority(card)
    if explicit is not None:
        return explicit, "explicitní Trello priorita"

    lowered = text.casefold()
    pm_repair = (
        any(term in lowered for term in ("ai-project-manager", "project manager", "orchestrator", "scheduler", "trello contract"))
        and any(term in lowered for term in ("oprav", "bug", "chyba", "nefung", "regres", "fix", "přetrvává"))
    )
    if pm_repair:
        return 5, "oprava vlastního PM/orchestrátoru nebo jeho workflow"
    if any(term in lowered for term in ("bezpeč", "security", "ztrát", "data loss", "produkč", "blokuj")):
        return 5, "bezpečnostní, produkční nebo blokující dopad"
    if any(term in lowered for term in ("live", "chyba", "bug", "nefung", "přetrvává", "regres", "error")):
        return 4, "potvrzený bug nebo regrese z live používání"
    if any(term in lowered for term in ("oprav", "fix", "urgent", "krit")):
        return 4, "opravný nebo naléhavý požadavek"
    if any(term in lowered for term in ("rozšíř", "implement", "přidat", "funkc")):
        return 3, "realizovatelná změna funkcionality"
    if any(term in lowered for term in ("budouc", "nápad", "research", "rešerš")):
        return 1, "budoucí nebo rešeršní práce"
    return max(0, min(5, default_priority)), "výchozí priorita bez silnějšího signálu"


def resolve_project_key(
    card: Mapping,
    text: str,
    project_paths: Optional[Mapping[str, str]] = None,
    card_project_keys: Optional[Mapping[str, str]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve identity only from an explicit card mapping or identity label.

    Titles and descriptions are intentionally never used to infer a repository.
    When an allowlist is supplied, unknown explicit identities fail closed too.
    """
    card_id = str(card.get("id") or "")
    title = str(card.get("name") or "")
    overrides = card_project_keys or {}
    explicit = overrides.get(card_id) or overrides.get(title)
    if explicit:
        explicit = str(explicit)
        if project_paths is not None and explicit not in project_paths:
            return None, "Explicitní projektová identita není v povolené mapě projektů."
        return explicit, None

    labels = {
        str(label.get("name") if isinstance(label, dict) else label).strip()
        for label in card.get("labels", []) or []
    }
    identity_labels = [label for label in labels if label and not _EXPLICIT_PRIORITY_RE.match(label)]
    if len(identity_labels) == 1:
        identity = identity_labels[0]
        if project_paths is not None and identity not in project_paths:
            return None, "Projektová identita z Trello štítku není v povolené mapě projektů."
        return identity, None
    if len(identity_labels) > 1:
        return None, "Inbox karta má více projektových identit; vyžaduje lidské rozhodnutí."

    return None, "Chybí explicitní projektová identita (neprioritní Trello štítek nebo schválená mapa karty)."


_GROUPS = (
    ("data stanice", ("bearing", "vzdálen", "země", "prefix", "pásm", "50mhz")),
    ("auto tune a hold", ("auto tune", "autotune", "hold", "kolébkov")),
    ("propagation a scoring", ("propagation", "šířen", "skoring", "scoring", "kp", "score")),
    ("DX Cluster poskytovatelé", ("dx cluster", "poskytovatel")),
    ("band-opening notifikace", ("notifik", "band-opening")),
)


def _sentences(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for sentence in _SENTENCE_RE.split(text):
        clean = sentence.strip().strip('"').strip()
        if not clean:
            continue
        key = re.sub(r"\s+", " ", clean).casefold()
        if key not in seen:
            seen.add(key)
            result.append(clean.rstrip("."))
    return result


def split_tasks(source_name: str, text: str, project_key: Optional[str]) -> tuple[PreparedTask, ...]:
    """Split materially different workstreams, retaining every source clause."""
    sentences = _sentences(text)
    assigned: set[int] = set()
    tasks: list[PreparedTask] = []
    prefix = project_key or source_name
    for scope, keywords in _GROUPS:
        indices = [
            index for index, sentence in enumerate(sentences)
            if index not in assigned and any(keyword in sentence.casefold() for keyword in keywords)
        ]
        if not indices:
            continue
        assigned.update(indices)
        body = ". ".join(sentences[index] for index in indices).strip() + "."
        tasks.append(
            PreparedTask(
                title=f"{prefix} — {scope}",
                task=body,
                next_step=f"Prověřit a implementovat rozsah: {scope}.",
                scope=scope,
            )
        )

    remainder = [sentence for index, sentence in enumerate(sentences) if index not in assigned]
    if remainder:
        body = ". ".join(remainder).strip() + "."
        # Keep the canonical source name when the card does not need splitting.
        # A synthetic suffix would break ordinary source/target idempotence.
        title = source_name if not tasks else f"{prefix} — další požadavky"
        tasks.append(
            PreparedTask(
                title=title,
                task=body,
                next_step="Rozdělit a ověřit zbývající požadavky proti projektu.",
                scope="další požadavky",
            )
        )
    if not tasks:
        tasks.append(
            PreparedTask(
                title=source_name or "Inbox úkol",
                task=text.strip(),
                next_step="Upřesnit první implementační krok.",
                scope="celý požadavek",
            )
        )
    return tuple(tasks)


def build_dod(tasks: tuple[PreparedTask, ...]) -> tuple[DoDItem, ...]:
    """Create a complete, phase-labelled DoD suitable for Ready admission."""
    scopes = ", ".join(task.scope for task in tasks)
    return (
        DoDItem(text=f"Implementovat připravené části Inbox požadavku: {scopes}.", phase="implementation"),
        DoDItem(text="Ai-orchestrator spustí cílené regresní testy a uvede konkrétní výsledek; nový commit není pro tento auditní bod vyžadován.", phase="audit"),
        DoDItem(text="Ověřit relevantní chování v živém prostředí a zapsat konkrétní důkaz.", phase="implementation"),
        DoDItem(text="Nezávislý audit ai-orchestrator vydá accepted / rejected verdikt.", phase="audit"),
    )


def prepare_inbox_card(
    card: Mapping,
    *,
    project_paths: Optional[Mapping[str, str]] = None,
    card_project_keys: Optional[Mapping[str, str]] = None,
    default_priority: int = 2,
    priority_override: Optional[tuple[int, str]] = None,
) -> InboxPreparation:
    source_name = str(card.get("name") or "Inbox úkol").strip()
    text = normalize_inbox_text(inbox_source_text(card) or source_name)
    project_key, human_reason = resolve_project_key(card, text, project_paths, card_project_keys)
    priority, priority_reason = priority_override or derive_priority(card, text, default_priority)
    raw_tasks = split_tasks(source_name, text, project_key)
    tasks = tuple(
        replace(
            task,
            priority=derive_priority(card, task.task, default_priority)[0],
            priority_reason=derive_priority(card, task.task, default_priority)[1],
        )
        for task in raw_tasks
    )
    return InboxPreparation(
        source_name=source_name,
        normalized_text=text,
        project_key=project_key,
        priority=priority,
        priority_reason=priority_reason,
        tasks=tasks,
        dod=build_dod(tasks),
        human_required_reason=human_reason,
    )
