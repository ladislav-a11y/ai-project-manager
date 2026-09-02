"""Validation and materialization of AI-prepared raw Trello Inbox work.

The production planner runs before a card is admitted to the governed
workflow. This module keeps the deterministic splitter for tests and
backwards-compatible callers, while accepting a validated AI task plan from
the read-only ai-orchestrator handoff.
When explicitly enabled by the production configuration, a genuinely new
unlabelled idea receives an isolated, source-ID-bound project identity under
the configured projects root; an ambiguous existing identity still fails
closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional

from .models import DoDItem


_WORD_RE = re.compile(r"[a-zA-Z0-9áčďéěíňóřšťúůýž]+", re.IGNORECASE)
_EXPLICIT_PRIORITY_RE = re.compile(r"^P([0-5](?:\.\d+)?)$", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\"(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ])")
_PM_DATA_BLOCK_RE = re.compile(r"<!--\s*PM-DATA.*?-->", re.DOTALL)


@dataclass(frozen=True)
class PreparedTask:
    """One independently dispatchable task derived from one Inbox card."""

    title: str
    task: str
    next_step: str
    scope: str
    priority: float = 2
    priority_reason: str = "výchozí priorita bez silnějšího signálu"
    # Zero-based indices in the AI plan.  A task may be dispatched only after
    # all listed sibling tasks are in Hotovo; priority orders only tasks that
    # are otherwise dependency-ready.
    depends_on: tuple[int, ...] = ()


@dataclass(frozen=True)
class InboxPreparation:
    """Validated, explainable preparation result for one raw Inbox card."""

    source_name: str
    normalized_text: str
    project_key: Optional[str]
    priority: float
    priority_reason: str
    tasks: tuple[PreparedTask, ...]
    dod: tuple[DoDItem, ...]
    human_required_reason: Optional[str] = None
    project_path: Optional[str] = None
    generated_project: bool = False


def task_execution_order(tasks: tuple[PreparedTask, ...] | list[PreparedTask]) -> tuple[int, ...]:
    """Validate and return a dependency-safe, priority-aware task order.

    Dependencies refer to the stable zero-based indices in the AI response.
    Kahn's algorithm makes cycles and out-of-range references fail closed.
    Among currently ready nodes, the higher priority wins; this preserves the
    business priority rule without ever running a dependent task first.
    """
    items = tuple(tasks)
    count = len(items)
    dependencies: dict[int, set[int]] = {}
    dependents: dict[int, set[int]] = {index: set() for index in range(count)}
    for index, task in enumerate(items):
        raw = task.depends_on or ()
        if not isinstance(raw, (tuple, list)):
            raise ValueError(f"task {index} dependencies must be an array")
        deps = set()
        for dependency in raw:
            if isinstance(dependency, bool) or not isinstance(dependency, int):
                raise ValueError(f"task {index} dependency index must be an integer")
            if dependency < 0 or dependency >= count or dependency == index:
                raise ValueError(f"task {index} has invalid dependency index {dependency}")
            deps.add(dependency)
            dependents[dependency].add(index)
        dependencies[index] = deps

    ready = [index for index, deps in dependencies.items() if not deps]
    order: list[int] = []
    while ready:
        ready.sort(key=lambda index: (-items[index].priority, index))
        index = ready.pop(0)
        order.append(index)
        for dependent in dependents[index]:
            dependencies[dependent].discard(index)
            if not dependencies[dependent]:
                ready.append(dependent)
    if len(order) != count:
        raise ValueError("AI Inbox task dependencies contain a cycle")
    return tuple(order)


def visible_inbox_description(card: Mapping) -> str:
    """Return only user-authored Inbox text, excluding the machine contract."""
    return _PM_DATA_BLOCK_RE.sub("", str(card.get("desc") or "")).strip()


def inbox_source_text(card: Mapping) -> str:
    """Return the human Inbox request, falling back to its title."""
    return visible_inbox_description(card) or str(card.get("name") or "").strip()


def _generated_project_identity(source_name: str, source_id: str) -> str:
    """Create a stable identity for a genuinely new Inbox project.

    The short immutable source-ID suffix prevents two similar ideas from
    silently sharing one checkout. This is only used after classification has
    established that the card is not an existing project or feedback item.
    """
    name = re.sub(r"^\s*P[0-5](?:\.\d+)?\s*[-–—:]\s*", "", source_name, flags=re.IGNORECASE)
    name = re.sub(r"^\s*budoucí projekt\s*[-–—:]\s*", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*\[[^\]]+\]\s*$", "", name).strip(" -–—")
    name = name or "Nový Inbox projekt"
    suffix = re.sub(r"[^a-zA-Z0-9]", "", source_id or "")[:8]
    return f"{name} [Inbox {suffix}]" if suffix else name


def _generated_project_path(project_key: str, projects_root: str) -> str:
    """Return a root-contained checkout path for an auto-created project."""
    root = Path(projects_root).expanduser().resolve()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", project_key.casefold()).strip("-") or "inbox-project"
    candidate = (root / slug).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("generated Inbox project path escaped AI_PM_PROJECTS_ROOT")
    return str(candidate)


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


def is_repair_request(text: str) -> bool:
    """Return whether text describes corrective work.

    Corrective work is a hard P5 signal. This helper is shared by Inbox
    intake and board maintenance so a lower source label cannot demote a
    confirmed repair.
    """
    lowered = (text or "").casefold()
    if re.search(r"\bbug\b", lowered):
        return True
    return any(
        re.search(
            rf"(?<![a-záčďéěíňóřšťúůýž]){re.escape(term)}",
            lowered,
            flags=re.IGNORECASE,
        )
        for term in ("oprav", "fix", "chyba", "nefung", "regres", "error", "rework", "náprav")
    )


def derive_priority(card: Mapping, text: str, default_priority: float = 2) -> tuple[float, str]:
    """Derive P5..P0 from explicit labels or a documented urgency rubric."""
    # A repair is always the highest operational priority. Evaluate this
    # before an inherited/explicit source label so intake cannot demote it.
    if is_repair_request(text):
        return 5, "oprava nebo potvrzená regrese; závazná nejvyšší priorita"
    explicit = _explicit_priority(card)
    if explicit is not None:
        return explicit, "explicitní Trello priorita"

    lowered = text.casefold()

    def has_term(term: str) -> bool:
        # Stems are intentional for Czech inflection (``oprav`` matches
        # ``oprava``), but must not match inside another word (``dopravy``).
        return re.search(
            rf"(?<![a-záčďéěíňóřšťúůýž]){re.escape(term)}",
            lowered,
            flags=re.IGNORECASE,
        ) is not None

    standalone_bug = bool(re.search(r"\bbug\b", lowered))
    mentions_pm = any(
        has_term(term)
        for term in (
            "ai-project-manager",
            "project manager",
            "orchestrator",
            "scheduler",
            "trello contract",
        )
    ) or bool(re.search(r"\bpm\b", lowered))
    pm_repair = mentions_pm and (
        any(
            has_term(term)
            for term in (
                "oprav",
                "chyba",
                "nefung",
                "regres",
                "fix",
                "přetrvává",
                "úprav",
            )
        )
        or standalone_bug
    )
    if pm_repair:
        return 5, "oprava vlastního PM/orchestrátoru nebo jeho workflow"
    if any(has_term(term) for term in ("bezpeč", "security", "ztrát", "data loss", "produkč", "blokuj")):
        return 5, "bezpečnostní, produkční nebo blokující dopad"
    if any(has_term(term) for term in ("live", "chyba", "nefung", "přetrvává", "regres", "error")) or standalone_bug:
        return 4, "potvrzený bug nebo regrese z live používání"
    if any(has_term(term) for term in ("oprav", "fix", "urgent", "krit")):
        return 4, "opravný nebo naléhavý požadavek"
    if any(
        has_term(term)
        for term in (
            "rozšíř",
            "implement",
            "přidat",
            "funkc",
            "zobraz",
            "vypoč",
            "výpoč",
            "dopln",
            "zachov",
            "umožn",
            "vrát",
            "přepoč",
            "notifik",
        )
    ):
        return 3, "realizovatelná změna funkcionality"
    if any(has_term(term) for term in ("budouc", "nápad", "research", "rešerš")):
        return 1, "budoucí nebo rešeršní práce"
    return max(0, min(5, default_priority)), "výchozí priorita bez silnějšího signálu"


def _unique_child_priorities(tasks: list[PreparedTask]) -> list[PreparedTask]:
    """Make priorities unique within one split while preserving P5..P0 bands.

    Decimal subpriorities are only introduced when two children have the same
    contextual base priority. They remain below the next integer band, so a
    P4 child still outranks every P3.xx child.
    """
    by_priority: dict[int, list[int]] = {}
    for index, task in enumerate(tasks):
        by_priority.setdefault(int(task.priority), []).append(index)
    result = list(tasks)
    for base, indices in by_priority.items():
        if len(indices) < 2:
            continue
        denominator = 10 ** len(str(len(indices) + 1))
        for rank, index in enumerate(indices, start=1):
            subpriority = round(base + rank / denominator, 6)
            result[index] = replace(
                result[index],
                priority=subpriority,
                priority_reason=(
                    f"{result[index].priority_reason}; pořadí podúkolu "
                    f"{rank}/{len(indices)} v prioritní úrovni P{base}"
                ),
            )
    return result


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


def _remainder_clauses(sentences: list[str]) -> list[str]:
    """Break otherwise-unclassified long requirements into atomic clauses.

    Inbox ideas commonly arrive as a paragraph followed by bullet points.
    Keeping all unmatched text in one ``další požadavky`` task made a large
    new project effectively one oversized Hermes handoff.  Bullets, lines,
    and semicolon-separated clauses are safe local structure; they do not
    invent content or merge distinct source cards.
    """
    clauses: list[str] = []
    for sentence in sentences:
        parts = re.split(r"\s*(?:[•▪◦]|\r?\n|;|\s[-–—]\s)\s*", sentence)
        clauses.extend(part.strip(" .:-") for part in parts if part.strip(" .:-"))
    return clauses


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

    remainder_sentences = [
        sentence for index, sentence in enumerate(sentences) if index not in assigned
    ]
    remainder = _remainder_clauses(remainder_sentences)
    if remainder:
        if len(remainder) == 1 and not tasks:
            # Keep the canonical source name when the card does not need
            # splitting. A synthetic suffix would break source idempotence.
            tasks.append(
                PreparedTask(
                    title=source_name,
                    task=remainder[0] + ".",
                    next_step="Upřesnit první implementační krok.",
                    scope="celý požadavek",
                )
            )
        else:
            for number, clause in enumerate(remainder, start=1):
                tasks.append(
                    PreparedTask(
                        title=f"{prefix} — požadavek {number}",
                        task=clause.rstrip(".") + ".",
                        next_step="Prověřit a implementovat tento samostatný požadavek.",
                        scope=f"požadavek {number}",
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
        DoDItem(text="Nezávislý audit ai-orchestratoru provede cílené regresní testy a uvede konkrétní výsledek; nový commit není pro tento auditní bod vyžadován.", phase="audit"),
        # Live/test verification is controller-owned evidence.  Keeping it
        # out of the implementation phase prevents Hermes (or any other
        # worker) from receiving a task it is not allowed to complete.
        DoDItem(text="Nezávislý audit ai-orchestratoru ověří relevantní chování v živém prostředí a zapíše konkrétní důkaz; nový commit není pro tento auditní bod vyžadován.", phase="audit"),
        DoDItem(text="Nezávislý audit ai-orchestrator vydá accepted / rejected verdikt.", phase="audit"),
    )


def prepare_inbox_card(
    card: Mapping,
    *,
    project_paths: Optional[Mapping[str, str]] = None,
    card_project_keys: Optional[Mapping[str, str]] = None,
    default_priority: int = 2,
    priority_override: Optional[tuple[int, str]] = None,
    projects_root: Optional[str] = None,
    allow_new_project: bool = False,
    planned_tasks: Optional[tuple[PreparedTask, ...]] = None,
) -> InboxPreparation:
    source_name = str(card.get("name") or "Inbox úkol").strip()
    text = normalize_inbox_text(inbox_source_text(card) or source_name)
    project_key, human_reason = resolve_project_key(card, text, project_paths, card_project_keys)
    project_path: Optional[str] = None
    generated_project = False
    if (
        allow_new_project
        and human_reason
        # A repair names an existing system boundary, but its title is not a
        # safe repository identity.  Never create a disposable slug checkout
        # for corrective work; require an explicit project label/mapping.
        and not is_repair_request(text)
        and not any(
            str(label.get("name") if isinstance(label, dict) else label).strip()
            and not _EXPLICIT_PRIORITY_RE.match(
                str(label.get("name") if isinstance(label, dict) else label).strip()
            )
            for label in card.get("labels", []) or []
        )
        and projects_root
    ):
        project_key = _generated_project_identity(source_name, str(card.get("id") or ""))
        project_path = _generated_project_path(project_key, projects_root)
        human_reason = None
        generated_project = True
    priority, priority_reason = priority_override or derive_priority(card, text, default_priority)
    raw_tasks = planned_tasks or split_tasks(source_name, text, project_key)
    if planned_tasks is not None:
        prefix = project_key or source_name
        raw_tasks = tuple(
            replace(
                task,
                title=(
                    task.title
                    if " — " in task.title
                    else f"{prefix} — {task.title or task.scope}"
                ),
            )
            for task in raw_tasks
        )
    # A source card's explicit P-label expresses the urgency of the Inbox
    # request as a whole. Once it is split into independent workstreams,
    # each child must be ranked from its own content; otherwise one inherited
    # P0 label makes every materially different task look identical. Keep
    # the neutral configured default when a child has no stronger contextual
    # signal. An explicit source P-label is still retained in the parent
    # metadata, but must not flatten independent child priorities.
    child_card = dict(card)
    child_card["labels"] = [
        label for label in (card.get("labels", []) or [])
        if str(label.get("name") if isinstance(label, dict) else label).strip().casefold()
        and not _EXPLICIT_PRIORITY_RE.match(
            str(label.get("name") if isinstance(label, dict) else label).strip()
        )
    ]
    prepared_tasks: list[PreparedTask] = []
    for task in raw_tasks:
        if planned_tasks is not None:
            prepared_tasks.append(
                replace(
                    task,
                    priority_reason=(
                        task.priority_reason
                        or "priorita přidělena AI Inbox plannerem"
                    ),
                )
            )
            continue
        child_priority, child_reason = derive_priority(
            child_card,
            f"{task.scope}: {task.task}",
            default_priority,
        )
        prepared_tasks.append(
            replace(task, priority=child_priority, priority_reason=child_reason)
        )
    tasks = tuple(_unique_child_priorities(prepared_tasks))
    return InboxPreparation(
        source_name=source_name,
        normalized_text=text,
        project_key=project_key,
        priority=priority,
        priority_reason=priority_reason,
        tasks=tasks,
        dod=build_dod(tasks),
        human_required_reason=human_reason,
        project_path=project_path,
        generated_project=generated_project,
    )
