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

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional

from .models import DoDItem


_WORD_RE = re.compile(r"[a-zA-Z0-9áčďéěíňóřšťúůýž]+", re.IGNORECASE)
_EXPLICIT_PRIORITY_RE = re.compile(r"^P([0-5](?:\.\d+)?)$", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\"(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ])")
# PM-DATA is machine-owned and must never cross the Inbox planner boundary.
# The end marker is optional on purpose: a partially written/legacy block must
# fail closed by removing everything from its opening marker onward instead of
# leaking arbitrary contract/history text into the next provider request.
_PM_DATA_BLOCK_RE = re.compile(r"<!--\s*PM-DATA.*?(?:-->|\Z)", re.DOTALL)
_WORKING_DIRECTORY_LINE_RE = re.compile(
    r"^\s*Pracovní\s+adresář\s*:\s*(?P<path>.*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

INBOX_AUDIT_EVIDENCE_TEXT = (
    "Nezávislý audit ai-orchestratoru ověří splnění implementačního DoD pomocí "
    "konkrétních důkazů relevantních pro povahu změny, zkontroluje skutečný stav "
    "checkoutu a podle potřeby použije statické kontroly, testy nebo live ověření; "
    "nerelevantní typ důkazu není povinný a nový commit není pro tento auditní bod "
    "vyžadován."
)
INBOX_AUDIT_VERDICT_TEXT = (
    "Nezávislý audit ai-orchestrator vydá accepted / rejected verdikt s konkrétním "
    "odůvodněním."
)
INBOX_READ_ONLY_AUDIT_EVIDENCE_TEXT = (
    "Nezávislý audit ai-orchestratoru ověří výsledek read-only verifikačního "
    "podúkolu proti skutečnému stavu bez změny souborů; nový commit není pro "
    "tento auditní bod vyžadován."
)

VERIFICATION_EVIDENCE_TYPES = (
    "static",
    "unit",
    "integration",
    "regression",
    "runtime",
    "gui",
    "config",
)
VERIFICATION_EVIDENCE_LABELS = {
    "static": "statická kontrola",
    "unit": "cílené unit testy",
    "integration": "integrační testy",
    "regression": "regresní testy",
    "runtime": "live/runtime ověření",
    "gui": "ověření Windows GUI",
    "config": "ověření platnosti a načtení konfigurace",
}

_READ_ONLY_VERIFICATION_MARKERS = (
    "do not modify any files",
    "must not modify files",
    "no files may be modified",
    "nesmí měnit soubory",
    "neměnit soubory",
    "bez změny souborů",
)

# Explicit Czech inflection variants for stable production project names.
# This is intentionally a small allowlist, not fuzzy matching.
_PROJECT_IDENTITY_ALIASES = {
    "Station Agent": (
        "station agent",
        "station agenta",
        "station agentu",
        "station agentovi",
        "station agentem",
        "station agentům",
        "station agentů",
    ),
    "AI Project Manager": (
        "ai project manager",
        "ai project manageru",
        "ai project managerem",
    ),
    "AI Orchestrator": (
        "ai orchestrator",
        "ai orchestratoru",
        "ai orchestrátoru",
        "ai-orchestrator",
        "ai-orchestratoru",
    ),
}


# The AI planner (see ``orchestrator_runner.build_inbox_planner_fn``) plans
# from exactly one Inbox source card. By default a source is an ordinary
# splittable request and the planner may describe several
# dependency-ordered tasks for it, mirroring the local deterministic
# ``split_tasks`` fallback below. A source card becomes bound to the
# stricter fail-closed one-task contract only when it carries the explicit
# ``[indivisible]`` marker (see ``is_explicit_indivisible_inbox_source``);
# that opt-in, never inferred from wording/length, is enforced before any
# card is written into Připraveno.
INDIVISIBLE_SOURCE_TASK_COUNT = 1

_INDIVISIBLE_SOURCE_MARKER_RE = re.compile(r"(?<!\w)\[\s*indivisible\s*\]", re.IGNORECASE)


def is_explicit_indivisible_inbox_source(card: Mapping) -> bool:
    """Return whether *card* carries the explicit ``[indivisible]`` marker.

    Detection is a literal, deterministic bracketed token in the card title
    or visible description - never inferred from the request's length,
    wording, or perceived complexity. Only a source card that opts in this
    way is bound to the fail-closed one-task contract; an ordinary Inbox
    source stays free to split into several AI-planned tasks.
    """
    title = str(card.get("name") or "")
    if _INDIVISIBLE_SOURCE_MARKER_RE.search(title):
        return True
    return bool(_INDIVISIBLE_SOURCE_MARKER_RE.search(visible_inbox_description(card)))


def enforce_indivisible_inbox_source_contract(tasks, *, indivisible: bool) -> Optional[str]:
    """Return a concrete Czech rejection reason, or ``None`` if the AI
    planner's task list honours the applicable Inbox source contract.

    ``indivisible`` must be the caller's own explicit determination (see
    ``is_explicit_indivisible_inbox_source``) - this function never infers
    it. A non-indivisible source only requires a non-empty task list; a
    source explicitly marked indivisible must describe exactly one task.
    Callers must fail closed on a non-``None`` result: leave the source card
    in Inbox and perform no Trello write, rather than materializing a
    contract-violating AI plan into Připraveno.
    """
    if not isinstance(tasks, (list, tuple)):
        return "Výstup AI planneru není pole úkolů."
    count = len(tasks)
    if count == 0:
        return "AI planner nevrátil žádný úkol."
    if indivisible and count != INDIVISIBLE_SOURCE_TASK_COUNT:
        return (
            f"AI planner vrátil {count} úkolů; zdrojová Inbox karta je "
            "explicitně označena jako nedělitelná ([indivisible]) a smí mít "
            "právě jeden úkol."
        )
    return None


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
    # Planner-provided per-task identity. When present it is authoritative
    # only after validation against the configured project allowlist.
    project_key: Optional[str] = None
    # Planner semantics are retained for traceability and later routing
    # decisions; they must not be silently discarded at the AO -> PM boundary.
    work_type: Optional[str] = None
    split_reason: Optional[str] = None
    verification: Optional["VerificationPlan"] = None
    source_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationPlan:
    """Machine-readable audit evidence guidance from the Inbox planner."""

    required: tuple[str, ...]
    acceptable: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "required": list(self.required),
            "acceptable": list(self.acceptable),
            "reason": self.reason,
        }


def is_read_only_verification(
    work_type: Optional[str], *texts: Optional[str]
) -> bool:
    """Identify an explicitly read-only Inbox research task.

    ``research`` alone is not enough: research may still produce a real
    artifact. The task must also explicitly forbid file changes, which makes
    it safe to route through the independent audit path instead of an
    implementation-agent dispatch.
    """
    if str(work_type or "").strip().casefold() != "research":
        return False
    combined = " ".join(str(text) for text in texts if text).casefold()
    return any(marker in combined for marker in _READ_ONLY_VERIFICATION_MARKERS)


def is_read_only_verification_task(task: PreparedTask) -> bool:
    return is_read_only_verification(
        task.work_type,
        task.task,
        task.next_step,
        task.split_reason,
    )


def prepared_task_handoff_text(task: PreparedTask, project_label: str) -> str:
    """Render a handoff without turning read-only verification into work."""
    action = "Ověřit" if is_read_only_verification_task(task) else "Implementovat"
    return (
        f"{action} tento samostatný rozsah v projektu {project_label}: "
        f"{task.task} Zachovat chování mimo tento rozsah."
    )


def _verification_audit_text(tasks: tuple[PreparedTask, ...]) -> Optional[str]:
    plans = [task.verification for task in tasks]
    if not plans or any(plan is None for plan in plans):
        return None
    required = tuple(dict.fromkeys(
        evidence for plan in plans for evidence in plan.required
    ))
    acceptable = tuple(dict.fromkeys(
        evidence for plan in plans for evidence in plan.acceptable
        if evidence not in required
    ))
    if not required:
        return None
    required_text = ", ".join(VERIFICATION_EVIDENCE_LABELS[item] for item in required)
    text = (
        "Nezávislý audit ai-orchestratoru provede minimálně: "
        f"{required_text}."
    )
    if acceptable:
        acceptable_text = ", ".join(
            VERIFICATION_EVIDENCE_LABELS[item] for item in acceptable
        )
        text += f" Doplňující nebo náhradní důkaz může být: {acceptable_text}."
    reasons = tuple(dict.fromkeys(plan.reason for plan in plans if plan.reason))
    if reasons:
        text += f" Důvod: {'; '.join(reasons)}."
    text += " Neproveditelný požadavek musí auditor konkrétně zdůvodnit."
    return text


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
    """Resolve an existing project without guessing across repositories.

    Explicit card mappings and identity labels remain authoritative.  An
    optional ``Pracovní adresář:`` declaration is accepted only when its
    normalized path matches exactly one configured checkout. If neither is
    present, one configured identity in the title is accepted (including the
    explicit Czech aliases above). A body-only match is accepted only when
    exactly one configured project is mentioned. Multiple or absent matches
    still fail closed.
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

    if project_paths is not None:
        declared_path, path_reason = _declared_working_directory(card)
        if path_reason:
            return None, path_reason
        if declared_path:
            normalized_declared = _normalized_project_path(declared_path)
            path_matches = [
                str(identity).strip()
                for identity, configured_path in project_paths.items()
                if _normalized_project_path(str(configured_path)) == normalized_declared
            ]
            if len(path_matches) == 1:
                return path_matches[0], None
            if len(path_matches) > 1:
                return None, "Pracovní adresář odpovídá více projektovým identitám."
            return None, "Pracovní adresář není v povolené mapě projektů."

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

    if project_paths is not None:
        title_matches = _configured_project_matches(title, project_paths)
        if len(title_matches) == 1:
            return title_matches[0], None
        if len(title_matches) > 1:
            return None, "Titulek Inbox karty obsahuje více projektových identit; vyžaduje lidské rozhodnutí."

        body_matches = _configured_project_matches(text, project_paths)
        if len(body_matches) == 1:
            return body_matches[0], None
        if len(body_matches) > 1:
            return None, "Popis Inbox karty obsahuje více projektových identit; vyžaduje lidské rozhodnutí."

    return None, "Chybí explicitní projektová identita (neprioritní Trello štítek nebo schválená mapa karty)."


def _declared_working_directory(card: Mapping) -> tuple[Optional[str], Optional[str]]:
    """Read the optional exact working-directory declaration from Inbox text."""
    description = visible_inbox_description(card)
    matches = list(_WORKING_DIRECTORY_LINE_RE.finditer(description))
    if len(matches) > 1:
        return None, "Inbox karta obsahuje více deklarací pracovního adresáře."
    if not matches:
        return None, None
    path = matches[0].group("path").strip()
    if not path:
        return None, "Deklarace pracovního adresáře nesmí být prázdná."
    return path, None


def _normalized_project_path(path: str) -> str:
    """Normalize a path for exact allowlist comparison on the host OS."""
    value = str(path or "").strip()
    try:
        return os.path.normcase(os.path.normpath(str(Path(value).expanduser().resolve())))
    except (OSError, RuntimeError):
        return os.path.normcase(os.path.normpath(os.path.abspath(value)))


def _project_key_form(value: str) -> str:
    """Normalize separators for a unique PM/AO identity comparison."""
    separated = str(value or "").casefold().replace("-", " ").replace("_", " ")
    return " ".join(_WORD_RE.findall(separated))


def _canonical_project_key(
    value: str,
    project_paths: Mapping[str, str],
) -> Optional[str]:
    """Map an AO identity to one exact PM allowlist key, or fail closed."""
    candidate = str(value or "").strip()
    keys = [str(key).strip() for key in project_paths if str(key).strip()]
    exact = [key for key in keys if key == candidate]
    if len(exact) == 1:
        return exact[0]
    folded = [key for key in keys if key.casefold() == candidate.casefold()]
    if len(folded) == 1:
        return folded[0]
    form = _project_key_form(candidate)
    if not form:
        return None
    normalized = [key for key in keys if _project_key_form(key) == form]
    return normalized[0] if len(normalized) == 1 else None


def _configured_project_matches(text: str, project_paths: Mapping[str, str]) -> list[str]:
    """Return configured identities explicitly named in *text*.

    Matching is bounded phrase matching, never fuzzy.  A one-word project
    key is ignored because ordinary prose would make it too easy to select
    the wrong checkout accidentally.
    """
    haystack = str(text or "").casefold()
    matches: list[str] = []
    for identity in project_paths:
        identity_text = str(identity or "").strip()
        if not identity_text or _EXPLICIT_PRIORITY_RE.match(identity_text):
            continue
        phrases = _PROJECT_IDENTITY_ALIASES.get(identity_text, (identity_text,))
        for phrase in phrases:
            normalized = str(phrase).casefold().replace("-", " ")
            words = re.findall(
                r"[a-z0-9áčďéěíňóřšťúůýž]+", normalized, re.IGNORECASE
            )
            if len(words) < 2:
                continue
            pattern = r"(?<!\w)" + r"\s+".join(
                re.escape(word) for word in words
            ) + r"(?!\w)"
            if re.search(pattern, haystack, flags=re.IGNORECASE):
                matches.append(identity_text)
                break
    return matches


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
    new project effectively one oversized provider handoff.  Bullets, lines,
    and semicolon-separated clauses are safe local structure; they do not
    invent content or merge distinct source cards.
    """
    clauses: list[str] = []
    for sentence in sentences:
        parts = re.split(r"\s*(?:[•▪◦]|\r?\n|;|\s[-–—]\s)\s*", sentence)
        clauses.extend(part.strip(" .:-") for part in parts if part.strip(" .:-"))
    return clauses


def _task_project_key(
    task: PreparedTask,
    source_key: Optional[str],
    project_paths: Optional[Mapping[str, str]],
) -> Optional[str]:
    """Resolve one subtask's project identity without overriding the planner.

    A planner-provided ``project_key`` is authoritative after allowlist
    validation. Legacy/deterministic tasks without one retain the historical
    bounded scope matching and source inheritance behavior.
    """
    if task.project_key:
        if project_paths is None:
            return task.project_key
        canonical = _canonical_project_key(task.project_key, project_paths)
        return canonical or source_key
    if not project_paths:
        return source_key
    matches = _configured_project_matches(f"{task.scope} {task.task}", project_paths)
    if len(matches) == 1:
        return matches[0]
    return source_key


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
    """Create a complete, phase-labelled DoD suitable for Ready admission.

    The planner defines what must be implemented, not which proof technique an
    independent auditor must use.  The auditor chooses evidence appropriate to
    the actual change: static checks, tests and live verification are tools,
    not universal mandatory gates.
    """
    if tasks and all(is_read_only_verification_task(task) for task in tasks):
        return (
            DoDItem(text=INBOX_READ_ONLY_AUDIT_EVIDENCE_TEXT, phase="audit"),
            DoDItem(text=INBOX_AUDIT_VERDICT_TEXT, phase="audit"),
        )
    scopes = ", ".join(task.scope for task in tasks)
    verification_text = _verification_audit_text(tasks)
    return (
        DoDItem(
            text=f"Implementovat připravené části Inbox požadavku: {scopes}.",
            phase="implementation",
        ),
        DoDItem(
            text=verification_text or INBOX_AUDIT_EVIDENCE_TEXT,
            phase="audit",
        ),
        DoDItem(text=INBOX_AUDIT_VERDICT_TEXT, phase="audit"),
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

    # AI planner identity is per-task and must survive materialization. If the
    # source itself has no identity but every planned task names an allowlisted
    # project, that is sufficient and must never fall through to generated
    # checkout creation. Conflicts/unknown keys fail closed.
    raw_planned_project_keys = {
        str(task.project_key).strip()
        for task in (planned_tasks or ())
        if task.project_key and str(task.project_key).strip()
    }
    if project_paths is None:
        planned_project_keys = set(raw_planned_project_keys)
        unknown_keys: list[str] = []
    else:
        planned_project_keys = set()
        unknown_keys = []
        for raw_key in sorted(raw_planned_project_keys):
            canonical = _canonical_project_key(raw_key, project_paths)
            if canonical is None:
                unknown_keys.append(raw_key)
            else:
                planned_project_keys.add(canonical)
    if planned_tasks is not None and (planned_project_keys or unknown_keys):
        if unknown_keys:
            if not (allow_new_project and not project_key and not planned_project_keys):
                project_key = None
                human_reason = (
                    "AI planner vrátil projektovou identitu mimo povolenou mapu projektů: "
                    + ", ".join(unknown_keys)
                )
        elif len(planned_project_keys) == 1:
            planner_key = next(iter(planned_project_keys))
            if project_key and project_key != planner_key:
                human_reason = (
                    "Projektová identita AI planneru je v konfliktu s explicitní identitou zdrojové karty."
                )
            else:
                project_key = planner_key
                human_reason = None
        elif project_key and project_key not in planned_project_keys:
            human_reason = (
                "Projektová identita zdrojové karty není obsažena v explicitních identitách AI planneru."
            )
        else:
            # A valid cross-project split may legitimately have no single
            # source-level project identity; each child remains explicitly bound.
            human_reason = None

    project_path: Optional[str] = None
    generated_project = False
    if (
        allow_new_project
        and human_reason
        and not planned_project_keys
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
    # A source card's urgency is the upper bound for AI-created child work.
    # The planner may order and differentiate siblings, but it may not turn a
    # normal feature/research request into P5 without an explicit corrective
    # signal in that child. This keeps the large AI intake useful while
    # preventing arbitrary planner numbers from overruling the deterministic
    # source-level priority rubric.
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
            child_text = f"{task.scope}: {task.task}"
            child_priority = float(task.priority)
            if is_repair_request(text) and child_priority < priority:
                child_priority = priority
                priority_reason_suffix = (
                    f"; zachována priorita opravného zdrojového zadání P{priority:g}"
                )
            elif not is_repair_request(child_text) and child_priority > priority:
                child_priority = priority
                priority_reason_suffix = (
                    f"; omezeno na prioritu zdrojového zadání P{priority:g}"
                )
            else:
                priority_reason_suffix = ""
            prepared_tasks.append(
                replace(
                    task,
                    priority=child_priority,
                    priority_reason=(
                        task.priority_reason + priority_reason_suffix
                    ).strip(),
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
    prepared_tasks = [
        replace(task, project_key=_task_project_key(task, project_key, project_paths))
        for task in prepared_tasks
    ]
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
