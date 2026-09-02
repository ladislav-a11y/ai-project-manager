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
import logging
import math
import re
from datetime import datetime, timezone
from typing import Optional

from .models import DoDItem, GitHubRef, GoogleDriveRef, ProjectRecord, ProjectStatus
from .inbox_preparation import derive_priority, is_repair_request
from .trello_client import MAX_TRELLO_DESC_CHARS
from .card_contract import (
    CURRENT_SCHEMA_VERSION,
    DOD_ROUTING_POLICY,
    GOVERNANCE_POLICY,
    CardContractError,
    dispatch_contract_issues,
    dod_contract_issues,
    migrate_and_validate,
    repair_incomplete_contract,
    unknown_fields,
)

logger = logging.getLogger("ai_project_manager")

BLOCK_START = "<!-- PM-DATA"
BLOCK_END = "-->"
BLOCK_RE = re.compile(
    re.escape(BLOCK_START) + r"\s*(.*?)\s*" + re.escape(BLOCK_END), re.DOTALL
)

# Any field can hold arbitrary agent-generated text (last_output, a
# checkpoint value, ...), and that text can very plausibly contain the
# literal substring "-->" (a diff, HTML/markdown, plain "before --> after"
# prose). Left unescaped, that would prematurely close the fenced block
# for the ".*?" search above, truncating the JSON mid-way, failing to
# parse, and silently wiping every field this block carries - checkpoint,
# last_output, stop_reason, retry_after, provider - defeating the whole
# point of Trello being the durable source of truth. A zero-width space
# is not something real content is expected to contain, so splitting
# "-->" with one is a safe, fully reversible escape.
_ZWSP = "\u200b"


def _escape_block_terminator(text: str) -> str:
    return text.replace(BLOCK_END, f"--{_ZWSP}>")


def _unescape_block_terminator(text: str) -> str:
    return text.replace(f"--{_ZWSP}>", BLOCK_END)


PRIORITY_LABEL_RE = re.compile(r"^P([0-5](?:\.\d+)?)$")
_VISIBLE_DOD_ITEM_RE = re.compile(r"\[[ xX]\]\s*([^\[]+?)(?=\s*\[[ xX]\]|\s*$)")

# One checklist item per line, Markdown-style ("- [ ] text" / "* [x] text",
# or a bare "[ ] text" with no leading bullet).
_CHECKLIST_LINE_RE = re.compile(r"^\s*(?:[-*]\s*)?\[([ xX])\]\s+(.+?)\s*[,;]?\s*$")
# Several bracketed items packed onto one line/paragraph, as the real
# Czech board sometimes writes a whole "DEFINITION OF DONE: [ ] a [ ] b"
# checklist inline rather than one item per line.
_CHECKLIST_INLINE_RE = re.compile(r"\[([ xX])\]\s*([^\[]+?)(?=\s*\[[ xX]\]|\s*$)")

# Audit-only DoD entries describe the result of the independent audit gate,
# rather than implementation work.  Trello's visible Markdown checklist has
# no separate phase field, so classify these result phrases on the first
# import and persist the phase in PM-DATA on every later sync.  Deliberately
# do not classify a bare occurrence of "audit" (for example "implement audit
# logging") as audit-only.
_AUDIT_DOD_RE = re.compile(
    # Unicode escapes keep this production matcher independent of the source
    # file/console code page. ``nezávisl*`` accepts Czech case endings while
    # still requiring the following audit noun, so "implement audit logging"
    # remains an implementation requirement.
    r"(?:\b(?:independent|independently|nez(?:a|\u00e1)visl\w*)\s+audit\w*\b)"
    r"|(?:\baudit\w*\b.{0,80}\b(?:accept(?:s|ed)?|pass(?:es|ed)?|"
    r"projd\w*|p[řr]ij\w*|schv[aá]l\w*)\b)"
    r"|(?:\bai-orchestrator\b.{0,120}\baccepted\s*/\s*rejected\b.{0,80}\b(?:verdict|verdikt)\b)",
    re.IGNORECASE,
)


def _dod_phase(text: str) -> str:
    """Classify a phase-less checklist item imported from visible Trello text."""
    return "audit" if _AUDIT_DOD_RE.search(text or "") else "implementation"


def _parse_checklist_items(text: str) -> list[tuple[str, bool]]:
    """Extract every ``[ ]``/``[x]`` checklist item from free-form text,
    in the order it appears, along with its checked state.

    Tries the one-item-per-line Markdown form first; falls back to
    scanning a line for several bracketed items packed together so
    neither authoring style silently drops items.
    """
    items: list[tuple[str, bool]] = []
    for line in (text or "").splitlines():
        line_match = _CHECKLIST_LINE_RE.match(line)
        if line_match:
            label = line_match.group(2).strip().rstrip(".,;")
            if label:
                items.append((label, line_match.group(1).lower() == "x"))
            continue
        for inline_match in _CHECKLIST_INLINE_RE.finditer(line):
            label = inline_match.group(2).strip().rstrip(".,;")
            if label:
                items.append((label, inline_match.group(1).lower() == "x"))
    return items


def _dedupe_checklist_items(items: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """Keep first occurrence order; a later duplicate is dropped rather
    than kept, so the same requirement quoted twice (e.g. once in the
    visible checklist, once again inside a structured task field) never
    turns into two Definition-of-Done entries."""
    seen: set = set()
    deduped: list[tuple[str, bool]] = []
    for text, checked in items:
        key = text.strip().casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append((text, checked))
    return deduped


def _merge_dod_items(project_dod_raw) -> Optional[list[DoDItem]]:
    if not project_dod_raw:
        return None

    merged: list[DoDItem] = []
    for raw_item in project_dod_raw:
        item = dict(raw_item)
        if "phase" not in item:
            item["phase"] = _dod_phase(item.get("text", ""))
        merged.append(DoDItem.from_dict(item))
    return merged


def _build_dod(notes: str, data: dict) -> list[DoDItem]:
    """The card's full Definition-of-Done checklist, with a clear,
    deterministic precedence: the visible checklist above PM-DATA first
    (in the order it appears, since that is what a human reads and what
    must never be silently reduced), then any explicit structured
    checklist item (orchestrator_ready_task, main_task, next_step,
    open_feedback, in that fixed order) not already covered by a visible
    item of the same text - so the same requirement stated in both
    places is never duplicated.
    """
    visible_items = _dedupe_checklist_items(_parse_checklist_items(notes))
    structured_sources = [
        data.get("orchestrator_ready_task") or "",
        data.get("main_task") or "",
        data.get("next_step") or "",
        *[fb for fb in (data.get("open_feedback") or []) if fb],
    ]
    structured_items = _dedupe_checklist_items(
        _parse_checklist_items("\n".join(structured_sources))
    )

    merged = list(visible_items)
    seen = {text.strip().casefold() for text, _ in visible_items}
    for text, checked in structured_items:
        key = text.strip().casefold()
        if key in seen:
            continue
        seen.add(key)
        merged.append((text, checked))

    return [
        DoDItem(text=text, checked=checked, phase=_dod_phase(text))
        for text, checked in merged
    ]

# Trello list name -> ProjectStatus.  Both the original English development
# board and the real Czech production board are supported explicitly.  An
# unknown list is rejected instead of silently becoming NEW and accidentally
# entering the scheduler.
LIST_NAME_TO_STATUS = {
    "Inbox": ProjectStatus.INBOX,
    "INBOX / Nápady": ProjectStatus.INBOX,
    "New": ProjectStatus.NEW,
    "Ready": ProjectStatus.READY,
    "Připraveno": ProjectStatus.READY,
    "In Progress": ProjectStatus.IN_PROGRESS,
    "Pracuje se": ProjectStatus.IN_PROGRESS,
    "Testing": ProjectStatus.TESTING,
    "Testování": ProjectStatus.TESTING,
    "Paused": ProjectStatus.PAUSED,
    "Čeká na AI": ProjectStatus.PAUSED,
    "Blocked": ProjectStatus.BLOCKED,
    "Done": ProjectStatus.DONE,
    "Hotovo": ProjectStatus.DONE,
    "Error": ProjectStatus.ERROR,
}
# Workflow order the physical board must preserve: Testování (audit-only,
# see orchestrator_handoff.build_audit_task) -> Čeká na AI (waiting on a
# provider/audit result) -> Pracuje se (normal implementation dispatch) ->
# Připraveno (ready to re-enter the queue). ``Testování`` is intentionally
# its own ProjectStatus (never folded into IN_PROGRESS) so the scheduler can
# tell "implementation in flight" and "awaiting ai-orchestrator's audit
# verdict" apart - see scheduler.is_schedulable/is_auditable.
STATUS_TO_LIST_CANDIDATES = {
    ProjectStatus.INBOX: ("INBOX / Nápady", "Inbox"),
    ProjectStatus.NEW: ("Připraveno", "New", "Ready"),
    ProjectStatus.READY: ("Připraveno", "Ready"),
    ProjectStatus.IN_PROGRESS: ("Pracuje se", "In Progress", "Čeká na AI"),
    ProjectStatus.TESTING: ("Testování", "Testing"),
    ProjectStatus.PAUSED: ("Čeká na AI", "Paused"),
    ProjectStatus.BLOCKED: ("Čeká na AI", "Blocked"),
    ProjectStatus.DONE: ("Hotovo", "Done"),
    ProjectStatus.ERROR: ("Čeká na AI", "Error"),
}
MAX_TRELLO_FEEDBACK_CHARS = 3500
MAX_TRELLO_LAST_OUTPUT_CHARS = 2500
TITLE_PRIORITY_RE = re.compile(r"^\s*P([0-5](?:\.\d+)?)(?:\s|[-—–:])", re.IGNORECASE)
TITLE_PRIORITY_PREFIX_RE = re.compile(r"^\s*P[0-5](?:\.\d+)?\s*(?:[-—–:]\s*)?", re.IGNORECASE)

_LEGACY_INBOX_AUDIT_TEXT = (
    "Ai-orchestrator spustí cílené regresní testy a uvede konkrétní výsledek; "
    "nový commit není pro tento auditní bod vyžadován."
)
_CURRENT_INBOX_AUDIT_TEXT = (
    "Nezávislý audit ai-orchestratoru provede cílené regresní testy a uvede "
    "konkrétní výsledek; nový commit není pro tento auditní bod vyžadován."
)
_LEGACY_INBOX_LIVE_TEXT = "Ověřit relevantní chování v živém prostředí a zapsat konkrétní důkaz."
_CURRENT_INBOX_LIVE_TEXT = (
    "Nezávislý audit ai-orchestratoru ověří relevantní chování v živém prostředí "
    "a zapíše konkrétní důkaz; nový commit není pro tento auditní bod vyžadován."
)
_LEGACY_INBOX_WORKER_SUFFIX = (
    " Zachovat chování mimo tento rozsah; dokončení doložit testem a relevantním live důkazem."
)
_CONTRACT_MIGRATION_MARKER = "_pm_contract_migration_required"


def priority_from_labels(labels: list[dict]) -> float:
    for label in labels or []:
        m = PRIORITY_LABEL_RE.match((label.get("name") or "").strip())
        if m:
            return float(m.group(1))
    return 0


def project_key_from_labels(labels: list[dict]) -> Optional[str]:
    """Return the card's one unambiguous project-identity label.

    This is deliberately a plain Trello label rather than anything parsed
    from the card's title - a title is free-form status prose that a work
    card's owner rewrites constantly (P0-P5 prefix, checkpoint notes,
    typo fixes) and is not required to ever mention which project/repo the
    card belongs to. A label, once set, survives every such edit untouched,
    which is what makes it durable enough to resolve a local checkout by
    (see orchestrator_runner.resolve_project_path).
    """
    identities = {
        (label.get("name") or "").strip()
        for label in labels or []
        if (label.get("name") or "").strip()
        and not PRIORITY_LABEL_RE.match((label.get("name") or "").strip())
    }
    if len(identities) > 1:
        # Preserve fail-closed loading: returning no usable identity lets the
        # pre-dispatch validator persist an actionable Trello reason and Slack
        # notice. Raising here would abort the whole board read before the
        # offending card could be updated.
        return None
    return next(iter(identities), None)


def priority_from_card(card: dict) -> float:
    """Use a P0..P5 label when present, otherwise the card-title prefix.

    The live board historically encoded priority only in names (for example
    ``P5 — AI Orchestrator``), so treating a missing label as P0 erased the
    ordering of every card.
    """
    for label in card.get("labels", []) or []:
        match = PRIORITY_LABEL_RE.match((label.get("name") or "").strip())
        if match:
            return float(match.group(1))
    match = TITLE_PRIORITY_RE.match(card.get("name", ""))
    return float(match.group(1)) if match else 0


def priority_label_name(priority: float) -> str:
    value = float(priority)
    if value.is_integer():
        return f"P{int(value)}"
    # At least two fractional digits keep P2.01 and P2.10 distinct. For a
    # queue larger than 99 cards, retain additional digits instead of
    # rounding two different priorities to the same visible label.
    rendered = f"{value:.6f}".rstrip("0").rstrip(".")
    whole, fractional = rendered.split(".", 1)
    if len(fractional) == 1:
        fractional += "0"
    return f"P{whole}.{fractional.zfill(2)}"


def _priority_prefixed_name(project: ProjectRecord) -> str:
    """Return the canonical visible name for a workflow card.

    Priority is operational metadata, but it must also remain visible in the
    title so a human can understand every governed queue without opening
    PM-DATA. Replace an old prefix rather than stacking prefixes after a
    reprioritization. This applies to terminal cards too: the title remains a
    durable human-readable record of the priority used for that work.
    """
    base_name = TITLE_PRIORITY_PREFIX_RE.sub("", str(project.name or ""), count=1).strip()
    return f"{priority_label_name(project.priority)} — {base_name or 'Inbox úkol'}"


def _workflow_visible_name(project: ProjectRecord) -> str:
    """Return the required title form for every non-Inbox workflow card."""
    if project.status == ProjectStatus.INBOX:
        return project.name
    return _priority_prefixed_name(project)


def status_from_list(list_id: Optional[str], list_id_to_name: dict[str, str]) -> ProjectStatus:
    name = list_id_to_name.get(list_id)
    if name not in LIST_NAME_TO_STATUS:
        raise ValueError(f"unmapped Trello list {name!r} (id={list_id!r})")
    return LIST_NAME_TO_STATUS[name]


def _parse_data_block(desc: str) -> dict:
    m = BLOCK_RE.search(desc or "")
    if not m:
        return {}
    try:
        return json.loads(_unescape_block_terminator(m.group(1)))
    except (ValueError, json.JSONDecodeError):
        return {}


def _raw_contract_data(desc: str) -> dict:
    """Parse without migration so maintenance can detect legacy cards."""
    match = BLOCK_RE.search(desc or "")
    if not match:
        return {}
    try:
        raw = json.loads(_unescape_block_terminator(match.group(1)))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CardContractError(f"invalid PM-DATA JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise CardContractError("PM-DATA must be a JSON object")
    return raw


def _parse_contract_data(desc: str) -> dict:
    """Strict PM read path: malformed/version-incompatible data is never reset."""
    return migrate_and_validate(_raw_contract_data(desc))


def _migrate_legacy_prepared_card(data: dict, card: dict, priority: int) -> bool:
    """Upgrade known PM-generated Inbox metadata before routing validation.

    A previous PM version emitted an audit-phase sentence that was
    controller-owned in meaning but lacked the explicit independent-audit
    marker required by the current Card Contract. Existing cards are repaired
    in memory first and persisted through the normal PM sync path.
    """
    changed = False
    for container in (
        data.get("dod"),
        (data.get("inbox_preparation") or {}).get("dod"),
    ):
        if not isinstance(container, list):
            continue
        for item in container:
            if not isinstance(item, dict):
                continue
            if item.get("text") == _LEGACY_INBOX_AUDIT_TEXT:
                item["text"] = _CURRENT_INBOX_AUDIT_TEXT
                item["phase"] = "audit"
                changed = True
            elif (
                item.get("text") == _LEGACY_INBOX_LIVE_TEXT
                and item.get("phase") == "implementation"
            ):
                # Old intake made the worker responsible for live evidence.
                # That is controller-owned audit work and could deadlock a
                # Hermes run even when the implementation itself was done.
                item["text"] = _CURRENT_INBOX_LIVE_TEXT
                item["phase"] = "audit"
                changed = True

    # Old split cards also embedded the audit/test obligation in the worker
    # handoff. Remove only this exact PM-generated suffix; never rewrite
    # user-authored task text by inference.
    worker_task = data.get("orchestrator_ready_task")
    if isinstance(worker_task, str) and worker_task.endswith(_LEGACY_INBOX_WORKER_SUFFIX):
        data["orchestrator_ready_task"] = worker_task[: -len(_LEGACY_INBOX_WORKER_SUFFIX)].rstrip()
        changed = True

    preparation = data.get("inbox_preparation")
    if isinstance(preparation, dict):
        # A priority label is authoritative after intake. Keep structured child
        # metadata aligned with it, and refresh its reason only when the same
        # contextual rubric confirms that priority.
        child_card = dict(card)
        child_card["labels"] = [
            label for label in (card.get("labels", []) or [])
            if str(label.get("name") if isinstance(label, dict) else label).strip().casefold()
            and not PRIORITY_LABEL_RE.match(
                str(label.get("name") if isinstance(label, dict) else label).strip()
            )
        ]
        derived_priority, derived_reason = derive_priority(
            child_card,
            f"{preparation.get('scope', '')}: {data.get('main_task', '')}",
            priority,
        )
        if preparation.get("task_priority") != priority:
            preparation["task_priority"] = priority
            changed = True
        if derived_priority == priority and preparation.get("priority_reason") != derived_reason:
            preparation["priority_reason"] = derived_reason
            changed = True

    if changed:
        data[_CONTRACT_MIGRATION_MARKER] = True
    return changed


def _validate_card_identity(data: dict, card: dict) -> None:
    """Refuse to process a contract bound to a different Trello card."""
    identity = data.get("card_identity")
    if not identity:
        return
    if identity["card_id"] != card.get("id"):
        raise CardContractError(
            f"card identity mismatch: contract id={identity['card_id']!r}, "
            f"actual id={card.get('id')!r}"
        )
    actual_url = card.get("url")
    if actual_url and _card_url_key(identity["card_url"]) != _card_url_key(actual_url):
        raise CardContractError(
            f"card identity mismatch: contract url={identity['card_url']!r}, "
            f"actual url={actual_url!r}"
        )


def _card_url_key(url: str) -> str:
    """Stable identity part of a Trello URL; ignore host and title slug."""
    # Rich-text Trello editors may preserve harmless surrounding whitespace
    # when a URL is converted to a smart-card link.  Identity comparison must
    # normalize that presentation detail, while still rejecting a different
    # card short-link.
    match = re.search(r"/c/([^/]+)", (url or "").strip())
    return match.group(1) if match else (url or "").rstrip("/")


def _render_data_block(data: dict) -> str:
    body = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
    return f"{BLOCK_START}\n{_escape_block_terminator(body)}\n{BLOCK_END}"


def _bound_contract_history(data: dict) -> dict:
    """Keep Trello writes below the API description limit.

    Audit notes can contain a full provider report and test output. Preserve
    the newest actionable feedback, but never let diagnostic history make the
    lifecycle update itself fail at the Trello API boundary.
    """
    bounded = dict(data)
    feedback = bounded.get("open_feedback")
    if isinstance(feedback, list):
        entries = [str(item) for item in feedback if item]
        if entries:
            combined = "\n\n".join(entries)
            if len(combined) > MAX_TRELLO_FEEDBACK_CHARS:
                combined = (
                    "[starší auditní historie zkrácena; zachován nejnovější důvod]\n"
                    + combined[-MAX_TRELLO_FEEDBACK_CHARS:]
                )
            bounded["open_feedback"] = [combined]
    last_output = bounded.get("last_output")
    if isinstance(last_output, str) and len(last_output) > MAX_TRELLO_LAST_OUTPUT_CHARS:
        bounded["last_output"] = (
            "[starší výstup zkrácen]\n" + last_output[-MAX_TRELLO_LAST_OUTPUT_CHARS:]
        )
    # The fixed per-field bounds above are not enough when a card also carries
    # a large checkpoint. Trim diagnostic history only. Task text and the
    # machine contract are never silently shortened because doing so could
    # change the work the agent receives or invalidate its checkpoint.
    prose_fields = ("open_feedback", "last_output")
    rendered_length = len(_render_data_block(bounded))
    while rendered_length > MAX_TRELLO_DESC_CHARS:
        changed = False
        for field in prose_fields:
            value = bounded.get(field)
            if isinstance(value, list) and value:
                text = str(value[-1])
                marker = "[zkráceno] "
                reduction = max(1000, rendered_length - MAX_TRELLO_DESC_CHARS)
                retained = max(0, len(text) - reduction - len(marker))
                shortened = marker + text[-retained:] if retained else marker.rstrip()
                if shortened == text:
                    continue
                bounded[field] = [shortened]
                changed = True
                break
            elif isinstance(value, str) and value:
                marker = "[zkráceno] "
                reduction = max(1000, rendered_length - MAX_TRELLO_DESC_CHARS)
                retained = max(0, len(value) - reduction - len(marker))
                shortened = marker + value[-retained:] if retained else marker.rstrip()
                if shortened == value:
                    continue
                bounded[field] = shortened
                changed = True
                break
        if not changed:
            # This is an unusually large task/DoD/checkpoint. Keep the
            # contract intact and fail closed rather than sending a payload
            # that Trello may truncate or reject.
            raise CardContractError(
                "refusing Trello write: PM-DATA exceeds the safe description "
                f"limit of {MAX_TRELLO_DESC_CHARS} characters after diagnostic truncation"
            )
        new_rendered_length = len(_render_data_block(bounded))
        if new_rendered_length >= rendered_length:
            raise CardContractError(
                "refusing Trello write: diagnostic truncation did not reduce PM-DATA"
            )
        rendered_length = new_rendered_length
    return bounded


def _bounded_description(visible_notes: str, data: dict) -> str:
    """Build a safe description without ever cutting the PM-DATA block."""
    rendered = _render_data_block(data)
    if len(rendered) > MAX_TRELLO_DESC_CHARS:
        raise CardContractError(
            "refusing Trello write: PM-DATA exceeds the safe description limit"
        )
    visible_notes = visible_notes.strip()
    if not visible_notes:
        return rendered
    separator = "\n\n"
    available = MAX_TRELLO_DESC_CHARS - len(rendered) - len(separator)
    if len(visible_notes) <= available:
        return f"{visible_notes}{separator}{rendered}"
    marker = "[viditelná historie zkrácena; PM-DATA zachován]\n"
    if available <= len(marker):
        return rendered
    return f"{marker}{visible_notes[-(available - len(marker)):]}{separator}{rendered}"


def _replace_data_block(desc: str, data: dict) -> str:
    """Return a description with one repaired contract block in place."""
    rendered = _render_data_block(data)
    if BLOCK_RE.search(desc or ""):
        return BLOCK_RE.sub(lambda _match: rendered, desc, count=1)
    visible = (desc or "").strip()
    return f"{visible}\n\n{rendered}" if visible else rendered


def _strip_data_block(desc: str) -> str:
    return BLOCK_RE.sub("", desc or "").strip()


def _legacy_dod_texts(project: ProjectRecord) -> list[str]:
    """Fallback item extraction for a project with no ``project.dod`` set
    (e.g. built directly rather than via ``project_from_card``) - kept so
    older callers still get a non-empty checklist on completion."""
    items: list[str] = []
    for source in (project.orchestrator_ready_task, project.main_task):
        for line in (source or "").splitlines():
            for match in _VISIBLE_DOD_ITEM_RE.finditer(line):
                item = match.group(1).strip().rstrip(".,;")
                if item and item not in items:
                    items.append(item)
    return items


def _completed_visible_notes(project: ProjectRecord) -> str:
    """Render the human audit trail required on every completed card:
    every original Definition-of-Done item, in its original order, each
    marked with its actual checked state - never silently reduced to a
    generic "done" line, and never all forced to checked regardless of
    what was actually verified.
    """
    dod_items = project.dod or [DoDItem(text=text, checked=True) for text in _legacy_dod_texts(project)]

    lines = [f"# {project.name}", "", "Stav: HOTOVO"]
    if project.completed_at:
        lines.append(f"Dokončeno: {_format_local_time(project.completed_at)}")
    lines.extend(["", "## Co bylo uděláno — Definition of Done"])
    if dod_items:
        lines.extend(f"- [{'x' if item.checked else ' '}] {item.text}" for item in dod_items)
    else:
        lines.append("- [x] Úkol dokončen a ověřen nadřazeným orchestrátorem.")
    if project.last_output:
        lines.extend(["", "## Poslední ověřený výstup", project.last_output[:3000]])
    return "\n".join(lines)


def _human_required_visible_notes(project: ProjectRecord) -> str:
    """The human-visible banner for a card currently flagged as needing a
    human (see daemon._run_recovery_pass). Written into the visible part of
    the description - never only inside the hidden PM-DATA JSON block - so
    a human scanning the board sees the same reason and concrete next step
    that was sent to Slack, without needing to inspect PM-DATA at all.
    """
    step = project.human_action_step or "Zkontrolujte kartu a rozhodněte další krok."
    return (
        "## ⚠ VYŽADUJE LIDSKÝ ZÁSAH\n"
        f"Důvod: {project.human_notified_reason}\n"
        f"Krok: {step}"
    )


def _format_local_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        # The production PM runs on the user's Prague Windows account. Using
        # the OS local zone avoids a separate tzdata dependency on Windows and
        # still applies the correct CET/CEST offset.
        return parsed.astimezone().strftime("%d.%m.%Y %H:%M %Z")
    except (AttributeError, TypeError, ValueError):
        return str(value)


def _waiting_visible_notes(project: ProjectRecord) -> str:
    reason = project.blocked_by or project.stop_reason or "Čeká na dostupnou AI nebo rozhodnutí."
    lines = ["## ⏳ ČEKÁ NA AI", f"Důvod: {reason}"]
    if project.waiting_since:
        lines.append(f"Čeká od: {_format_local_time(project.waiting_since)}")
    if project.retry_after:
        lines.append(f"Další automatický pokus: {_format_local_time(project.retry_after)}")
    if project.next_step:
        lines.append(f"Další krok: {project.next_step}")
    if project.human_action_step:
        lines.append(f"Potřebný zásah: {project.human_action_step}")
    return "\n".join(lines)


def project_from_card(card: dict, list_id_to_name: dict[str, str]) -> ProjectRecord:
    """Build a ProjectRecord snapshot from a raw Trello card dict."""
    raw_desc = card.get("desc", "")
    data = _parse_contract_data(raw_desc)
    _validate_card_identity(data, card)
    notes = _strip_data_block(raw_desc)

    if not any([
        data.get("main_task"),
        data.get("orchestrator_ready_task"),
        data.get("next_step"),
        data.get("open_feedback"),
    ]) and notes:
        data["main_task"] = notes
        data["orchestrator_ready_task"] = notes
    priority = priority_from_card(card)
    _migrate_legacy_prepared_card(data, card, priority)
    status = status_from_list(card.get("list_id"), list_id_to_name)

    # Some boards intentionally use one physical list (notably Czech
    # ``Ceka na AI``) for several logical states. The list remains the
    # authoritative workflow location, while this structured value
    # disambiguates PAUSED/BLOCKED/ERROR after a write/read round trip.
    # Older cards have no lifecycle_status and retain the legacy mapping.
    stored_status = data.get("lifecycle_status")
    if stored_status:
        try:
            candidate = ProjectStatus(stored_status)
        except (TypeError, ValueError):
            candidate = None
        list_name = list_id_to_name.get(card.get("list_id"))
        if candidate is not None and list_name in STATUS_TO_LIST_CANDIDATES[candidate]:
            status = candidate

    # A card in Hotovo is terminal.  If a write failed after the lifecycle
    # transition (for example while moving the card), do not carry the stale
    # implementation/audit-wait reason into the next maintenance write.
    if status == ProjectStatus.DONE:
        data["stop_reason"] = None
        data["blocked_by"] = None

    # The physical list is authoritative for lifecycle (see
    # ProjectRecord.is_blocked) - once a card sits in Ready/In Progress/Done
    # it is no longer blocked no matter what. A leftover ``blocked_by`` from
    # before the card was moved must not linger in memory (it would confuse
    # any code reading the field directly instead of ``is_blocked``) or be
    # written back to Trello unchanged - see maintain_board_contract, which
    # persists this same clearing to the card the next time it runs.
    blocked_by = data.get("blocked_by")
    if status in {ProjectStatus.READY, ProjectStatus.IN_PROGRESS, ProjectStatus.TESTING, ProjectStatus.DONE}:
        blocked_by = None

    github_repo = GitHubRef.from_dict(data.get("github_repo"))
    google_drive_ref = GoogleDriveRef.from_dict(data.get("google_drive_ref"))

    # Once a Definition-of-Done checklist has been derived for this card it
    # is persisted verbatim in the structured block (see
    # card_updates_from_project) and reused as-is - the visible portion of
    # the description is blanked out again on every non-DONE sync (see
    # below), so re-parsing "notes" on every read would silently lose every
    # item and checked state the moment the card is synced back once.  Only
    # a card the Project Manager has never synced (no "dod" in PM-DATA yet)
    # is parsed fresh from its visible checklist and structured fields.
    stored_dod = _merge_dod_items(data.get("dod"))
    dod = stored_dod if stored_dod is not None else _build_dod(notes, data)

    # DoD routing is part of the Trello contract, not an agent preference.
    # The physical workflow location is authoritative, so reject a card
    # before it can be selected from Ready/In Progress (or incorrectly sent
    # through the audit path) when its text assigns controller-only checks to
    # an implementation agent.
    if status in {ProjectStatus.READY, ProjectStatus.IN_PROGRESS, ProjectStatus.TESTING}:
        routing_issues = (
            dispatch_contract_issues(dod)
            if status == ProjectStatus.IN_PROGRESS
            else dod_contract_issues(dod)
        )
        if routing_issues:
            raise CardContractError("unsafe DoD routing: " + "; ".join(routing_issues))

    # Fold valid orchestrator checkpoint indices into the exact DoD list. A
    # stale index from an older checklist is ignored and normalized away on
    # the next write, so it cannot falsely imply completion.
    if dod and "completed_dod_indices" in data.get("checkpoint", {}):
        from .orchestrator_handoff import apply_dod_progress

        checkpoint = dict(data.get("checkpoint", {}))
        apply_dod_progress(ProjectRecord(name=card.get("name", ""), dod=dod), checkpoint)
        data["checkpoint"] = checkpoint

    # A card in Hotovo with an incomplete DoD is corrupt workflow state. Keep
    # unfinished implementation work in Pracuje se; audit-only incompletion
    # belongs in Testování. Never expose either state as done.
    if status == ProjectStatus.DONE and dod and not all(item.checked for item in dod):
        incomplete_implementation = any(
            item.phase == "implementation" and not item.checked for item in dod
        )
        status = ProjectStatus.IN_PROGRESS if incomplete_implementation else ProjectStatus.TESTING
        if incomplete_implementation:
            data["stop_reason"] = "terminal card had incomplete implementation DoD; rework required"
            data["next_step"] = (
                "Dokončit nesplněné implementační body a následně kartu znovu předat do Testování."
            )
        else:
            data["stop_reason"] = "terminal card had incomplete audit DoD; awaiting ai-orchestrator audit"
            data["next_step"] = "Doplnit auditní důkaz a znovu provést nezávislý audit."

    # Preserve the meaning of the PM-generated safety return across a
    # restart.  This is deliberately exact: free-form human stop reasons
    # must never be guessed as an audit return.
    extra_data = unknown_fields(data)
    limit_reason = str(data.get("stop_reason") or "").casefold()
    if status == ProjectStatus.IN_PROGRESS and "provider session limit hit" in limit_reason:
        # Older PM runs could persist the provider-limit reason but leave the
        # card in Pracuje se. Repair that stale state on read, without
        # guessing from arbitrary human text.
        status = ProjectStatus.PAUSED
        extra_data.setdefault("resume_status", ProjectStatus.READY.value)
    if status == ProjectStatus.IN_PROGRESS and str(data.get("stop_reason") or "").startswith("audit odložen:"):
        extra_data["returned_from_testing"] = True

    activity = card.get("last_activity_at")
    completed_at = (data.get("completed_at") or activity) if status == ProjectStatus.DONE else None
    waiting_since = data.get("waiting_since") or (
        activity if status in {ProjectStatus.PAUSED, ProjectStatus.BLOCKED, ProjectStatus.ERROR} else None
    )

    return ProjectRecord(
        # The leading P<n> is a visible Trello metadata prefix, not part of
        # the logical project identity used for provider prompts, response
        # matching, or project-path resolution.
        name=TITLE_PRIORITY_PREFIX_RE.sub("", str(card.get("name", "")), count=1).strip(),
        priority=priority,
        status=status,
        main_task=data.get("main_task", ""),
        open_feedback=list(data.get("open_feedback", [])),
        next_step=data.get("next_step", ""),
        orchestrator_ready_task=data.get("orchestrator_ready_task", ""),
        last_output=data.get("last_output", ""),
        checkpoint=dict(data.get("checkpoint", {})),
        blocked_by=blocked_by,
        stop_reason=data.get("stop_reason"),
        retry_after=data.get("retry_after"),
        status_updated_at=data.get("status_updated_at") or activity,
        waiting_since=waiting_since,
        completed_at=completed_at,
        recovery_attempts=int(data.get("recovery_attempts") or 0),
        review_at=data.get("review_at"),
        human_notified_reason=data.get("human_notified_reason"),
        human_action_step=data.get("human_action_step"),
        dod=dod,
        github_repo=github_repo,
        google_drive_ref=google_drive_ref,
        trello_card_id=card.get("id"),
        trello_list_id=card.get("list_id"),
        trello_card_url=card.get("url"),
        provider=data.get("provider"),
        project_key=project_key_from_labels(card.get("labels")),
        extra_data=extra_data,
    )


def card_updates_from_project(project: ProjectRecord, list_name_to_id: dict[str, str], notes: str = "") -> dict:
    """Build the ``update_card``/``create_card`` kwargs that write a
    ProjectRecord back onto its Trello card.

    ``notes`` is preserved free text that stays visible above the
    machine-readable block (e.g. a human-friendly summary).
    """
    data = dict(project.extra_data)
    # This transient marker tells maintenance that the read path repaired a
    # legacy card, but it must never become part of the durable contract.
    data.pop(_CONTRACT_MIGRATION_MARKER, None)
    # Fresh live Trello readback is evidence for the audit prompt only. It is
    # intentionally transient: persisting it would duplicate board metadata
    # inside PM-DATA and can push an already large contract over Trello's limit.
    data.pop("live_trello_readback", None)
    data.update({
        "schema_version": CURRENT_SCHEMA_VERSION,
        "governance": GOVERNANCE_POLICY,
        "dod_routing_policy": DOD_ROUTING_POLICY,
        "card_identity": (
            {"card_id": project.trello_card_id, "card_url": project.trello_card_url}
            if project.trello_card_id and project.trello_card_url else None
        ),
        "lifecycle_status": project.status.value,
        "main_task": project.main_task,
        "open_feedback": list(project.open_feedback),
        "next_step": project.next_step,
        "orchestrator_ready_task": project.orchestrator_ready_task,
        "last_output": project.last_output,
        "checkpoint": dict(project.checkpoint),
        "blocked_by": project.blocked_by,
        "stop_reason": project.stop_reason,
        "retry_after": project.retry_after,
        "status_updated_at": project.status_updated_at,
        "waiting_since": project.waiting_since,
        "completed_at": project.completed_at,
        "recovery_attempts": project.recovery_attempts,
        "review_at": project.review_at,
        "human_notified_reason": project.human_notified_reason,
        "human_action_step": project.human_action_step,
        "dod": [item.to_dict() for item in project.dod],
        "github_repo": project.github_repo.to_dict() if project.github_repo else None,
        "google_drive_ref": project.google_drive_ref.to_dict() if project.google_drive_ref else None,
        "provider": project.provider,
    })
    if project.status in {ProjectStatus.READY, ProjectStatus.IN_PROGRESS, ProjectStatus.TESTING}:
        routing_issues = (
            dispatch_contract_issues(project.dod)
            if project.status == ProjectStatus.IN_PROGRESS
            else dod_contract_issues(project.dod)
        )
        if routing_issues:
            raise CardContractError("refusing lifecycle write with unsafe DoD routing: " + "; ".join(routing_issues))
    visible_notes = notes.strip()
    if not visible_notes and project.status == ProjectStatus.DONE:
        visible_notes = _completed_visible_notes(project)

    if project.human_notified_reason and project.status == ProjectStatus.BLOCKED:
        banner = _human_required_visible_notes(project)
        visible_notes = f"{banner}\n\n{visible_notes}" if visible_notes else banner
    elif project.status in {ProjectStatus.PAUSED, ProjectStatus.BLOCKED, ProjectStatus.ERROR}:
        visible_notes = _waiting_visible_notes(project)

    data = _bound_contract_history(data)
    desc = _bounded_description(visible_notes, data)

    candidates = STATUS_TO_LIST_CANDIDATES[project.status]
    list_name = next((name for name in candidates if name in list_name_to_id), None)
    if list_name is None:
        raise ValueError(
            f"board has no list for status {project.status.value!r}; expected one of {candidates!r}"
        )
    list_id = list_name_to_id[list_name]

    # The project-identity label (see project_key_from_labels) must survive
    # every sync alongside the priority label, or a card's stable identity
    # would be silently wiped the very first time its status/priority
    # changes and this function writes the card's labels back.
    labels = [priority_label_name(project.priority)]
    if project.project_key:
        labels.append(project.project_key)

    # Priority must be visible in the title for every governed workflow card,
    # not only for cards created by Inbox intake. This prevents a stale title
    # from making a card in Pracuje se look like a different task after a
    # reprioritization or provider handoff.
    visible_name = _workflow_visible_name(project)

    return {
        "name": visible_name,
        "desc": desc,
        "list_id": list_id,
        "labels": labels,
    }


def build_list_maps(client) -> tuple[dict[str, str], dict[str, str]]:
    """Return (list_id -> name, name -> list_id) for the client's board."""
    lists = client.list_lists()
    id_to_name = {lst["id"]: lst["name"] for lst in lists}
    name_to_id = {lst["name"]: lst["id"] for lst in lists}
    return id_to_name, name_to_id


def fetch_all_projects(
    client,
    exclude_list_names: tuple[str, ...] = ("Inbox", "INBOX / Nápady"),
) -> list[ProjectRecord]:
    """Fetch every project card from the board (excluding Inbox, which
    holds unclassified raw input rather than assigned projects)."""
    id_to_name, _ = build_list_maps(client)
    projects: list[ProjectRecord] = []
    for list_id, name in id_to_name.items():
        if name in exclude_list_names:
            continue
        for card in client.list_cards(list_id):
            try:
                projects.append(project_from_card(card, id_to_name))
            except CardContractError as exc:
                logger.error(
                    "skipping unsafe Trello card id=%s name=%r: %s",
                    card.get("id"), card.get("name"), exc,
                )
    return projects


_AUDIT_REJECTION_MARKERS = (
    "ai-orchestrator audit rejected",
    "auditor odmítl",
)
_AUDIT_REJECTED_INDICES_RE = re.compile(
    r"DoD index(?:es|\(es\))?\s*\[([^\]]*)\]", re.IGNORECASE
)
_STRUCTURED_AUDIT_REJECTION_RE = re.compile(
    r"(?:^|[;\n])\s*\d+\s*:\s*REJECT\b", re.IGNORECASE
)
_IMPLEMENTATION_REWORK_MARKERS = (
    "card must return to pracuje se",
    "return to in_progress",
    "returned to in_progress",
    "implementation rework",
    "rework required",
)
_TERMINAL_ACCEPTANCE_MARKERS = (
    "independent audit accepted",
    "audit accepted",
)


def _has_newer_terminal_acceptance(project: ProjectRecord, raw: dict) -> bool:
    """Keep terminal cards whose later output explicitly proves completion.

    ``open_feedback`` is retained as historical audit evidence.  It can still
    contain an older rejection after a later controller finalization accepted
    the same card, so its presence alone is not an unresolved rejection.
    """
    last_output = str(raw.get("last_output") or "").casefold()
    if any(marker in last_output for marker in _AUDIT_REJECTION_MARKERS):
        return False
    if not any(marker in last_output for marker in _TERMINAL_ACCEPTANCE_MARKERS):
        return False
    finalization = (project.checkpoint or {}).get("finalization")
    return (
        isinstance(finalization, dict)
        and finalization.get("done") is True
        and bool(project.dod)
        and all(item.checked for item in project.dod)
    )


def _repair_terminal_incomplete_dod(project: ProjectRecord) -> bool:
    """Reopen a falsely terminal card when any DoD item is still open."""
    if project.status != ProjectStatus.DONE:
        return False
    incomplete_implementation = [
        item for item in project.dod
        if item.phase == "implementation" and not item.checked
    ]
    incomplete_audit = [
        item for item in project.dod
        if item.phase == "audit" and not item.checked
    ]
    if incomplete_implementation:
        project.stop_reason = "terminal card had incomplete implementation DoD; rework required"
        project.next_step = (
            "Dokončit nesplněné implementační body a následně kartu znovu předat do Testování."
        )
        project.transition_to(ProjectStatus.IN_PROGRESS)
        return True
    if incomplete_audit:
        project.stop_reason = "terminal card had incomplete audit DoD; awaiting ai-orchestrator audit"
        project.next_step = "Doplnit auditní důkaz a znovu provést nezávislý audit."
        project.transition_to(ProjectStatus.TESTING)
        return True
    return False


def _repair_terminal_test_dod_routing(project: ProjectRecord, raw: dict) -> bool:
    """Migrate completed cards whose test execution was misclassified.

    Historical cards may have completed implementation items that ask the
    agent to run tests.  When a later controller finalization proves the card
    complete, preserve that evidence but rewrite only those items as explicit
    controller-owned audit evidence.
    """
    if project.status not in {ProjectStatus.DONE, ProjectStatus.IN_PROGRESS, ProjectStatus.TESTING}:
        return False
    if project.status != ProjectStatus.DONE and raw.get("lifecycle_status") != ProjectStatus.DONE.value:
        return False
    changed = False
    for item in project.dod:
        issues = dod_contract_issues([item])
        text = item.text.casefold()
        test_text = any(marker in text for marker in ("test", "pytest", "syntaxe", "suite"))
        if item.phase == "audit":
            if not test_text or not any("phase='audit' but does not identify" in issue for issue in issues):
                continue
            item.text = f"Nezávislý audit ai-orchestratoru ověří, že {item.text[0].lower() + item.text[1:]}"
            if "nový commit není" not in item.text.casefold():
                item.text += "; nový commit není pro tento auditní bod vyžadován."
            changed = True
            continue
        if item.phase != "implementation" or not any(
            "requests test execution in implementation" in issue for issue in issues
        ):
            continue
        item.phase = "audit"
        if "ai-orchestrator" not in item.text.casefold():
            item.text = f"ai-orchestrator ověří, že {item.text[0].lower() + item.text[1:]}"
        if "nový commit není" not in item.text.casefold():
            item.text += "; nový commit není pro tento auditní bod vyžadován."
        changed = True
    return changed


def _repair_terminal_audit_rejection(project: ProjectRecord, raw: dict) -> bool:
    """Reopen a terminal card carrying an explicit unresolved audit rejection.

    A rejected audit is authoritative evidence from ai-orchestrator, not a
    PM inference. Older PM runs could still write ``done`` after persisting
    that rejection. Repair that impossible state before any terminal card
    is treated as complete: reopen implementation rejections to ``Pracuje
    se`` and keep audit-only rejections in ``Testování``.
    """
    if project.status != ProjectStatus.DONE:
        return False
    feedback = raw.get("open_feedback")
    if not isinstance(feedback, list):
        return False
    entries = [str(item).strip() for item in feedback if str(item).strip()]
    if not entries:
        return False
    combined = "\n".join(entries).casefold()
    explicit_rejection = (
        any(marker in combined for marker in _AUDIT_REJECTION_MARKERS)
        or raw.get("return_reason") == "audit_rejected"
        or bool(_STRUCTURED_AUDIT_REJECTION_RE.search("\n".join(entries)))
    )
    if not explicit_rejection:
        return False
    if _has_newer_terminal_acceptance(project, raw):
        return False

    rejected_indices: list[int] = []
    for match in _AUDIT_REJECTED_INDICES_RE.finditer("\n".join(entries)):
        for value in match.group(1).split(","):
            value = value.strip()
            if value.isdigit() and int(value) not in rejected_indices:
                rejected_indices.append(int(value))
    valid_indices = [
        index for index in rejected_indices
        if 0 <= index < len(project.dod)
    ]
    implementation_rejected = [
        index for index in valid_indices
        if project.dod[index].phase == "implementation"
    ]
    if not implementation_rejected and any(
        marker in combined for marker in _IMPLEMENTATION_REWORK_MARKERS
    ):
        implementation_rejected = [
            index for index, item in enumerate(project.dod)
            if item.phase == "implementation"
        ]
    if implementation_rejected:
        for index in implementation_rejected:
            project.dod[index].checked = False
        checkpoint = dict(project.checkpoint or {})
        if "completed_dod_indices" in checkpoint:
            checkpoint["completed_dod_indices"] = [
                index for index in checkpoint.get("completed_dod_indices", [])
                if index not in implementation_rejected
            ]
        project.checkpoint = checkpoint
        project.mark_returned_from_testing("audit_rejected")
        project.stop_reason = (
            "ai-orchestrator audit rejected implementation DoD; rework required"
        )
        project.next_step = (
            "Provést nápravnou implementaci podle otevřeného auditního feedbacku "
            "a následně znovu předat kartu do Testování."
        )
        project.transition_to(ProjectStatus.IN_PROGRESS)
    else:
        project.stop_reason = (
            "ai-orchestrator audit rejected audit evidence; awaiting re-audit"
        )
        project.next_step = (
            "Doplnit konkrétní auditní důkaz a znovu provést nezávislý audit."
        )
        project.transition_to(ProjectStatus.TESTING)
    return True


def maintain_board_contract(client) -> list[str]:
    """Idempotently migrate safe cards and enforce workflow/order.

    Invalid or future contracts are never written. Their diagnostics are
    returned to the daemon so an operator can be notified without risking
    data loss.
    """
    id_to_name, _ = build_list_maps(client)
    issues: list[str] = []
    active_projects: list[ProjectRecord] = []
    inbox_names = {"Inbox", "INBOX / Nápady"}
    for list_id, list_name in id_to_name.items():
        if list_name in inbox_names:
            continue
        for card in client.list_cards(list_id):
            try:
                raw = _raw_contract_data(card.get("desc", ""))
                try:
                    project = project_from_card(card, id_to_name)
                except CardContractError as original_error:
                    repaired_data, repaired_fields = repair_incomplete_contract(raw)
                    if not repaired_fields:
                        raise original_error
                    repaired_card = dict(card)
                    repaired_card["desc"] = _replace_data_block(
                        card.get("desc", ""), repaired_data
                    )
                    project = project_from_card(repaired_card, id_to_name)
                    logger.warning(
                        "repairing incomplete Card Contract for card %s (%r): %s",
                        card.get("id"), card.get("name"), ", ".join(repaired_fields),
                    )
                incomplete_dod_repaired = _repair_terminal_incomplete_dod(project)
                if incomplete_dod_repaired:
                    logger.warning(
                        "reopening terminal card with incomplete DoD card=%s name=%r",
                        card.get("id"), card.get("name"),
                    )
                dod_routing_repaired = _repair_terminal_test_dod_routing(project, raw)
                if dod_routing_repaired:
                    logger.warning(
                        "migrating terminal card test DoD to audit routing "
                        "card=%s name=%r",
                        card.get("id"), card.get("name"),
                    )
                audit_rejection_repaired = _repair_terminal_audit_rejection(project, raw)
                if audit_rejection_repaired:
                    logger.warning(
                        "reopening terminal card with unresolved audit rejection "
                        "card=%s name=%r status=%s",
                        card.get("id"), card.get("name"), project.status.value,
                    )
                if _repair_terminal_test_dod_routing(project, raw):
                    dod_routing_repaired = True
                # A card already in Ready with all implementation work done
                # and an outstanding audit belongs in Testování. Normalize
                # that state before scheduling can ever move it to Pracuje se;
                # this is local routing only and spends no provider tokens.
                implementation_items = [item for item in project.dod if item.phase == "implementation"]
                audit_items = [item for item in project.dod if item.phase == "audit"]
                if (
                    project.status == ProjectStatus.READY
                    and implementation_items
                    and all(item.checked for item in implementation_items)
                    and any(not item.checked for item in audit_items)
                ):
                    project.transition_to(ProjectStatus.TESTING)
                    project.stop_reason = "implementation DoD complete; awaiting ai-orchestrator audit"
                # A non-terminal card with every DoD item checked is an
                # impossible intermediate state: only an accepted,
                # ai-orchestrator-owned audit may close a card. Recover such
                # stale/hidden cards into the explicit audit gate instead of
                # leaving a falsely complete card in the implementation queue.
                if (
                    project.status in {ProjectStatus.READY, ProjectStatus.IN_PROGRESS}
                    and project.dod
                    and all(item.checked for item in project.dod)
                ):
                    project.transition_to(ProjectStatus.TESTING)
                    project.stop_reason = "all DoD items were checked before audit; awaiting ai-orchestrator audit"
                if project.status == ProjectStatus.IN_PROGRESS:
                    active_projects.append(project)
                identity = raw.get("card_identity")
                preparation = project.extra_data.get("inbox_preparation")
                dependency_metadata_needs_migration = False
                if isinstance(preparation, dict):
                    # Older split cards predate dependency-aware intake. An
                    # absent field means no dependency was declared by that
                    # historical plan; make that fact explicit so every
                    # current workflow card has the same machine-readable
                    # contract. New AI plans are validated before admission.
                    if "depends_on_subtask_indices" not in preparation:
                        preparation["depends_on_subtask_indices"] = []
                        dependency_metadata_needs_migration = True
                    if "execution_order" not in preparation:
                        preparation["execution_order"] = int(
                            preparation.get("subtask_index") or 0
                        )
                        dependency_metadata_needs_migration = True
                migration_needs_write = any((
                    raw.get("schema_version") != CURRENT_SCHEMA_VERSION,
                    raw.get("governance") != GOVERNANCE_POLICY,
                    raw.get("dod_routing_policy") != DOD_ROUTING_POLICY,
                    not identity,
                    (
                        card.get("name") != _workflow_visible_name(project)
                        and project.status != ProjectStatus.INBOX
                    ),
                    dependency_metadata_needs_migration,
                    bool(project.extra_data.get(_CONTRACT_MIGRATION_MARKER)),
                    not raw.get("status_updated_at") and bool(card.get("last_activity_at")),
                ))
                workflow_needs_write = any((
                    raw.get("lifecycle_status") != project.status.value,
                    project.returned_from_testing and raw.get("returned_from_testing") is not True,
                    project.status == ProjectStatus.DONE and not raw.get("completed_at"),
                    project.status != ProjectStatus.DONE and bool(raw.get("completed_at")),
                    project.status == ProjectStatus.DONE and bool(raw.get("stop_reason")),
                    project.status in {ProjectStatus.PAUSED, ProjectStatus.BLOCKED, ProjectStatus.ERROR}
                    and not raw.get("waiting_since"),
                    # Stale blocking metadata: project_from_card already
                    # cleared project.blocked_by in memory because the
                    # physical list is Ready/In Progress/Done, but the raw
                    # card still has the old value - persist the clearing.
                    bool(raw.get("blocked_by")) and not project.blocked_by,
                ))
                # Hotovo is terminal evidence. Loading it through the current
                # in-memory migration keeps reads compatible, but schema,
                # policy, title, identity and dependency normalization alone
                # must not rewrite its Trello description. Only a concrete
                # lifecycle repair (for example missing completion evidence)
                # makes a terminal write necessary.
                needs_write = workflow_needs_write or incomplete_dod_repaired or dod_routing_repaired or (
                    project.status != ProjectStatus.DONE and migration_needs_write
                )
                if needs_write:
                    sync_project_to_trello(client, project)
            except CardContractError as exc:
                message = f"card {card.get('id')} ({card.get('name')!r}): {exc}"
                logger.error("board maintenance skipped unsafe %s", message)
                issues.append(message)

    # The workflow has one active implementation slot. Preserve the newest
    # corrective return first: an audit may have just returned a card to
    # Pracuje se while an older corrective card is still occupying the slot.
    # The older card must be deferred, not the newly rejected work. For
    # ordinary active cards, priority remains the tie-breaker.
    if len(active_projects) > 1:
        def active_slot_key(project: ProjectRecord) -> tuple:
            if project.returned_from_testing:
                stamp = project.status_updated_at or ""
                try:
                    parsed_stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    if parsed_stamp.tzinfo is None:
                        parsed_stamp = parsed_stamp.replace(tzinfo=timezone.utc)
                    return (0, -parsed_stamp.timestamp(), -project.priority, project.name.casefold())
                except (TypeError, ValueError, OverflowError, OSError):
                    # A legacy return without a valid timestamp still beats
                    # normal work, but yields to timestamped corrective work.
                    return (0, float("inf"), -project.priority, project.name.casefold())
            return (1, 0, -project.priority, project.name.casefold())

        active_projects.sort(
            key=active_slot_key
        )
        keeper = active_projects[0]
        for project in active_projects[1:]:
            project.transition_to(ProjectStatus.READY)
            project.extra_data["workflow_deferred"] = f"single_active_card:{keeper.trello_card_id}"
            try:
                sync_project_to_trello(client, project)
            except CardContractError as exc:
                message = f"card {project.trello_card_id} ({project.name!r}): {exc}"
                logger.error("cannot defer active card safely %s", message)
                issues.append(message)

    # sync_project_to_trello sorts touched lists; untouched lists still need
    # deterministic order, but sort_list_cards itself performs no writes when
    # their current order already matches.
    for list_id, list_name in id_to_name.items():
        if list_name not in inbox_names:
            sort_list_cards(client, list_id, id_to_name=id_to_name)
    return issues


def sort_list_cards(client, list_id: str, id_to_name: Optional[dict[str, str]] = None) -> None:
    """Maintain visible board order after every PM write.

    Active queues are ordered by business priority (P5 highest). Completed
    work is ordered by its persisted completion timestamp, newest first.
    """
    if id_to_name is None:
        id_to_name, _ = build_list_maps(client)
    list_name = id_to_name.get(list_id)
    cards = client.list_cards(list_id)
    parsed = []
    for card in cards:
        try:
            parsed.append((card, project_from_card(card, id_to_name)))
        except CardContractError as exc:
            logger.error("not sorting list %r because card %s is unsafe: %s", list_name, card.get("id"), exc)
            return
    if list_name in {"Hotovo", "Done"}:
        parsed.sort(
            key=lambda pair: pair[1].completed_at or pair[0].get("last_activity_at") or "",
            reverse=True,
        )
    else:
        parsed.sort(key=lambda pair: (-pair[1].priority, pair[1].name.casefold()))
    desired_ids = [card["id"] for card, _project in parsed]
    current_ids = [card["id"] for card in cards]
    if current_ids == desired_ids:
        return
    for card, _project in reversed(parsed):
        client.update_card(card["id"], position="top")


def sync_project_to_trello(client, project: ProjectRecord, notes: str = "") -> dict:
    """Push a ProjectRecord's current state back onto its Trello card.

    This is the single write path back to the source of truth: status,
    priority, checkpoint, last output, next step, feedback, blocked_by
    and provider all get persisted onto the card in one update.
    """
    _, name_to_id = build_list_maps(client)
    updates = card_updates_from_project(project, name_to_id, notes=notes)
    if project.trello_card_id:
        current = client.get_card(project.trello_card_id)
        if project.trello_card_url and current.get("url") and (
            _card_url_key(current["url"]) != _card_url_key(project.trello_card_url)
        ):
            raise CardContractError(
                f"refusing Trello write for {project.name!r}: expected card URL "
                f"{project.trello_card_url!r}, actual {current['url']!r}"
            )
        try:
            current_data = _parse_contract_data(current.get("desc", ""))
        except CardContractError as original_error:
            repaired_data, repaired_fields = repair_incomplete_contract(
                _raw_contract_data(current.get("desc", ""))
            )
            if not repaired_fields:
                raise original_error
            logger.warning(
                "repairing incomplete Card Contract before sync for card %s (%r): %s",
                current.get("id"), current.get("name"), ", ".join(repaired_fields),
            )
            current_data = repaired_data
        _validate_card_identity(current_data, current)
        # Trello production can reject a single PUT that combines the large
        # contract description/labels with an idList transition (400).  Keep
        # the contract write and lifecycle move separate: the card content
        # must persist first, and only then may the card change workflow list.
        card = client.update_card(
            project.trello_card_id,
            name=updates["name"],
            desc=updates["desc"],
            labels=updates["labels"],
        )
        if current.get("list_id") != updates["list_id"]:
            card = client.update_card(
                project.trello_card_id,
                list_id=updates["list_id"],
            )
    else:
        card = client.create_card(
            updates["list_id"],
            updates["name"],
            desc=updates["desc"],
            labels=updates["labels"],
        )

    # Creating a project from Inbox assigns its durable Trello identity.
    # Keep that identity on the in-memory record immediately: the same
    # scheduler tick may run and sync this project again, and without the
    # card ID that second sync would create a duplicate card.
    project.trello_card_id = card.get("id", project.trello_card_id)
    project.trello_list_id = card.get("list_id", project.trello_list_id)
    project.trello_card_url = card.get("url", project.trello_card_url)
    # A create cannot know its Trello identity before Trello allocates it.
    # Bind the new card immediately, then read it back through the strict
    # parser so a bad response or accidental cross-card write cannot hide.
    if not _parse_contract_data(card.get("desc", "")).get("card_identity"):
        bound = card_updates_from_project(project, name_to_id, notes=notes)
        card = client.update_card(
            project.trello_card_id,
            name=bound["name"], desc=bound["desc"], labels=bound["labels"],
        )
        if card.get("list_id") != bound["list_id"]:
            card = client.update_card(
                project.trello_card_id,
                list_id=bound["list_id"],
            )
    verified = client.get_card(project.trello_card_id)
    verified_data = _parse_contract_data(verified.get("desc", ""))
    _validate_card_identity(verified_data, verified)
    if verified.get("list_id") != updates["list_id"]:
        raise CardContractError(
            f"Trello read-back list mismatch for {project.name!r}: "
            f"expected {updates['list_id']!r}, got {verified.get('list_id')!r}"
        )
    sort_list_cards(client, verified["list_id"])
    return verified
