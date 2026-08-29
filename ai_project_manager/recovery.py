"""Unattended blocked-task recovery: a periodic revisit pass over
``ProjectStatus.BLOCKED`` projects, independent of the normal priority
scheduler (see scheduler.py, which never picks a blocked project at all).

Without this, a card that ever became blocked would stay blocked forever
- nothing in the normal tick ever looks at it again. This module gives
every blocked card a periodic, rate-limited second look that:

  1. classifies *why* it is blocked (``classify_block``-equivalent, see
     ``_classify_recoverable_cause``) into one of: a provider/protocol
     error that has likely since been fixed, a temporary external
     blockage that may have cleared, missing/inconsistent PM-DATA that
     can be safely rederived from another field on the same card, a bad
     or incomplete task specification with nothing left to derive from,
     or an explicit human-required condition (credentials, approval,
     ...);
  2. only ever auto-repairs metadata that is safely derivable from other
     fields already on the same card - it never invents task content;
  3. requeues (clears ``blocked_by``, returns to ``IN_PROGRESS``) on a
     successful classification/repair, preserving ``priority`` and
     ``checkpoint`` untouched;
  4. otherwise leaves the project ``BLOCKED`` with a concrete, actionable
     ``blocked_by`` reason a human can act on;
  5. is rate-limited by ``review_at`` (an exponential backoff keyed off
     ``recovery_attempts``) so a card that keeps re-blocking is not
     rescanned every tick, and is permanently forced to human-required
     once ``max_attempts`` auto-recovery cycles have been spent without
     the project staying unblocked - this is what bounds the loop.

``recovery_attempts`` only increments when a repair actually requeues the
project; it is reset to 0 by the caller once a requeued project makes
real forward progress (see ``runner.run_once``), so a card that is
genuinely fixed does not carry a stale near-exhausted counter forever.

Every blocked project's ``blocked_by``/``stop_reason`` is also self-
healed (see ``_degarble_legacy_reason``) before classification: a
previous version of this module wrote its own synthesized diagnosis
back into ``blocked_by``, so a card left blocked long enough would
accumulate arbitrarily deep "blocked reason (blocked reason (...) does
not match...) does not match..." nesting. That mutation is gone, but a
card that already carries this corruption from before the fix needs to
recover on its own next pass instead of staying garbled forever.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Optional

from .models import ProjectRecord, ProjectStatus

DEFAULT_MAX_ATTEMPTS = 5
_BASE_BACKOFF = timedelta(minutes=10)
_MAX_BACKOFF = timedelta(hours=6)


class BlockCause(str, Enum):
    PROVIDER_ERROR_RESOLVED = "provider_error_resolved"
    TRANSIENT_EXTERNAL = "transient_external"
    MISSING_OR_INCONSISTENT_DATA = "missing_or_inconsistent_data"
    BAD_TASK_SPEC = "bad_task_spec"
    HUMAN_REQUIRED = "human_required"


@dataclass
class RecoveryOutcome:
    project_name: str
    # Trello permits duplicate card titles. Carry the immutable card ID so
    # callers can persist the exact recovered record rather than collapsing
    # same-named cards into a name-keyed mapping.
    trello_card_id: Optional[str]
    cause: Optional[BlockCause]
    # "requeued" | "human_required" | "deferred"
    action: str
    reason: str
    attempts: int
    # The concrete action a human should take, set only for
    # action == "human_required" - carried alongside ``reason`` so a
    # caller (see daemon._run_recovery_pass) can put both the "why" and
    # the "what to do" into the Slack message and the visible Trello card
    # text without re-deriving either from ``reason`` text.
    step: Optional[str] = None


# Explicit signals that no automatic action can safely resolve this -
# credentials/approval/legal/manual-decision language. Checked before any
# auto-recoverable classification, so an obviously human matter is never
# mistaken for a stale provider error just because it also happens to
# mention e.g. a timeout.
_HUMAN_REQUIRED_PATTERNS = re.compile(
    r"credential|secret|api[ _-]?key|token expired|manuáln|schválen|"
    r"rozhodnut[ií] člověka|human (?:input|review|approval|decision)|"
    r"potřebuje? (?:člověka|schválení)|legal|security review|"
    r"přístup(?:ová)? práva|permission denied|access denied|repeated failure",
    re.IGNORECASE,
)

# A provider/protocol failure signature that is plausibly a bug already
# fixed by a later code change, or a blip that has since cleared - worth
# one optimistic retry rather than assuming it needs a human.
_PROVIDER_PROTOCOL_ERROR_PATTERNS = re.compile(
    r"timeout|timed out|connection (?:reset|refused|error)|network error|"
    r"protocol error|unexpected eof|econnreset|http 5\d\d|server error|"
    r"temporarily unavailable|service unavailable|internal error",
    re.IGNORECASE,
)

# A dependency on something outside this project's own control that is
# only ever blocking *for a while* - also worth an optimistic retry.
_TRANSIENT_EXTERNAL_PATTERNS = re.compile(
    r"waiting (?:for|on)|čeká na|external (?:service|dependency|system)|"
    r"upstream|third[- ]party|závislost|blokován[ao]? (?:jinou|externí)",
    re.IGNORECASE,
)

_PLACEHOLDER_TASK_PATTERNS = re.compile(r"^(?:tbd|todo|wip|\?+|tba|xxx|n/?a)$", re.IGNORECASE)


def _is_placeholder(text: Optional[str]) -> bool:
    stripped = (text or "").strip()
    return not stripped or bool(_PLACEHOLDER_TASK_PATTERNS.match(stripped))


# One-time repair for reasons corrupted by a previous version of this module
# (see _mark_human_required) that wrote its own synthesized diagnostic text
# back into ``blocked_by``: the next recovery tick then reclassified using
# that already-wrapped text as input and wrapped it again, nesting these
# exact literal fragments arbitrarily deep on any card that stayed blocked
# across enough ticks (live-confirmed on the real board: cards degraded to
# several KB of "blocked reason (blocked reason (...) does not match...)
# does not match..." text). The current code no longer produces this, but
# already-corrupted Trello cards need to self-heal on their own, not stay
# garbled forever - these substrings never legitimately appear in a human-
# or orchestrator-authored blocked reason, so stripping every copy of them
# is safe.
_LEGACY_WRAP_SUFFIX = ") does not match a known auto-recoverable pattern; needs human triage"
_LEGACY_WRAP_PREFIX = "blocked reason ("
_LEGACY_HUMAN_PREFIX = "requires a human decision/credentials: "
_LEGACY_CORRUPTION_MARKERS = (_LEGACY_WRAP_SUFFIX, _LEGACY_HUMAN_PREFIX)


def _dedup_consecutive(items: list) -> list:
    out: list = []
    for item in items:
        if not out or out[-1] != item:
            out.append(item)
    return out


def _degarble_legacy_reason(text: Optional[str]) -> Optional[str]:
    """Recover the original short reason from legacy nested-wrapper text.

    A cheap no-op (single ``in`` scan, no allocation) for the overwhelming
    majority of calls where ``text`` was never corrupted in the first
    place - safe to call unconditionally on every blocked project's
    ``blocked_by``/``stop_reason`` on every recovery tick.
    """
    if not text or not any(marker in text for marker in _LEGACY_CORRUPTION_MARKERS):
        return text
    cleaned = text.replace(_LEGACY_WRAP_SUFFIX, "").replace(_LEGACY_WRAP_PREFIX, "")
    cleaned = cleaned.replace(_LEGACY_HUMAN_PREFIX, "")
    # Stray unmatched parens left behind by the removed wrapper literals.
    cleaned = cleaned.replace("(", " ").replace(")", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # The old wrapper joined blocked_by+stop_reason at every nesting level,
    # so the same original word or sentence ends up repeated back-to-back -
    # once at the word level (e.g. a bare one-word reason repeated many
    # times), once at the sentence level (a longer reason repeated whole) -
    # collapse both.
    cleaned = " ".join(_dedup_consecutive(cleaned.split(" ")))
    # Preserve the original's own trailing period, if it had one, instead
    # of fabricating punctuation that was never actually part of the
    # short original reason (e.g. a bare "blocked" or a credentials
    # sentence with no trailing ".").
    had_trailing_period = cleaned.endswith(".")
    sentences = _dedup_consecutive([s.strip() for s in cleaned.split(".") if s.strip()])
    if not sentences:
        return "unspecified (legacy blocked reason could not be recovered)"
    cleaned = ". ".join(sentences)
    if had_trailing_period:
        cleaned += "."
    return cleaned


def default_backoff(attempt: int) -> timedelta:
    """Exponential backoff keyed off the attempt number (1-indexed),
    capped so a persistently blocked card is still revisited at least
    every ``_MAX_BACKOFF``."""
    attempt = max(1, attempt)
    # Cap the exponent itself before computing the power. A few dozen
    # doublings already dwarf _MAX_BACKOFF, so letting a large ``attempt``
    # grow the exponent unbounded serves no purpose and would otherwise
    # overflow timedelta's internal representation (OverflowError) for a
    # sufficiently large input.
    capped_exponent = min(attempt - 1, 32)
    delay = _BASE_BACKOFF * (2 ** capped_exponent)
    return min(delay, _MAX_BACKOFF)


def _is_due(project: ProjectRecord, now: datetime) -> bool:
    if not project.review_at:
        return True
    try:
        review_at = datetime.fromisoformat(project.review_at)
    except (TypeError, ValueError):
        # PM-DATA comes from an external, user-editable Trello card. A
        # malformed value must not abort the whole scheduler tick. Treat it
        # like an invalid timestamp and review the blocked card now; the
        # recovery outcome will normalize or clear the value when persisted.
        return True
    if review_at.tzinfo is None:
        review_at = review_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now >= review_at


def _classify_recoverable_cause(project: ProjectRecord, reason_text: str) -> Optional[BlockCause]:
    real_main = not _is_placeholder(project.main_task)
    real_next = not _is_placeholder(project.next_step)
    real_ready = not _is_placeholder(project.orchestrator_ready_task)
    real_feedback = any(not _is_placeholder(item) for item in project.open_feedback)

    if not real_ready:
        # orchestrator_ready_task itself is the field ai-orchestrator is
        # actually handed (see orchestrator_handoff._goal_text); if it is
        # missing/placeholder but real content exists elsewhere on the
        # card, that is a safely rederivable inconsistency. If nothing
        # anywhere on the card has real content, there is nothing to
        # derive from at all - a genuinely bad/incomplete spec.
        if real_main or real_next or real_feedback:
            return BlockCause.MISSING_OR_INCONSISTENT_DATA
        return BlockCause.BAD_TASK_SPEC

    if reason_text and _PROVIDER_PROTOCOL_ERROR_PATTERNS.search(reason_text):
        return BlockCause.PROVIDER_ERROR_RESOLVED
    if reason_text and _TRANSIENT_EXTERNAL_PATTERNS.search(reason_text):
        return BlockCause.TRANSIENT_EXTERNAL
    return None


def _attempt_repair(project: ProjectRecord, cause: BlockCause) -> tuple[bool, str]:
    """Try to safely resolve ``cause``. Returns (repaired, note) - ``note``
    is either an explanation of what was auto-repaired (on success) or an
    actionable reason a human needs to see (on failure). Never invents
    task content: it only ever copies real text already present on
    another field of the same card."""
    if cause == BlockCause.PROVIDER_ERROR_RESOLVED:
        return True, f"provider/protocol error looks resolved ({project.blocked_by or project.stop_reason})"
    if cause == BlockCause.TRANSIENT_EXTERNAL:
        return True, f"temporary external block may have cleared ({project.blocked_by or project.stop_reason})"
    if cause == BlockCause.MISSING_OR_INCONSISTENT_DATA:
        source = next(
            (
                text.strip()
                for text in (project.main_task, project.next_step, *project.open_feedback)
                if not _is_placeholder(text)
            ),
            None,
        )
        if source:
            project.orchestrator_ready_task = source
            return True, "derived missing orchestrator_ready_task from other card fields"
        return False, "PM-DATA is inconsistent and no other field has usable task text; needs human review"
    if cause == BlockCause.BAD_TASK_SPEC:
        return False, (
            "task specification is empty or a placeholder across main_task, orchestrator_ready_task, "
            "next_step and open_feedback; needs a human to reformulate the task"
        )
    return False, "blocked reason does not match a known auto-recoverable pattern; needs human triage"


_REPAIR_FAILURE_STEPS = {
    BlockCause.MISSING_OR_INCONSISTENT_DATA: (
        "Doplňte platné zadání úkolu (main_task nebo orchestrator_ready_task) na kartě - "
        "žádné pole na kartě aktuálně neobsahuje použitelný text."
    ),
    BlockCause.BAD_TASK_SPEC: (
        "Doplňte konkrétní zadání do pole „Co má AI udělat“ (orchestrator_ready_task) na kartě - "
        "je prázdné nebo obsahuje jen zástupný text."
    ),
}
_DEFAULT_REPAIR_FAILURE_STEP = "Proveďte ruční kontrolu karty a rozhodněte další krok."
_EXHAUSTED_ATTEMPTS_STEP = (
    "Zkontrolujte příčinu poslední blokace na kartě, opravte ji a ručně přesuňte kartu zpět "
    "do 'Pracuje se' (nebo vymažte pole blocked_by), aby mohlo pokračovat automatické zpracování."
)
_HUMAN_PATTERN_STEP = (
    "Doplňte chybějící přístupové údaje/schválení popsané v důvodu a poté kartu ručně "
    "odblokujte (přesuňte ji mimo Blocked)."
)
_UNKNOWN_CAUSE_STEP = (
    "Proveďte ruční kontrolu karty - důvod blokace neodpovídá žádnému známému "
    "automaticky řešitelnému vzoru - a rozhodněte další krok."
)


def _mark_human_required(
    project: ProjectRecord,
    now: datetime,
    backoff: Callable[[int], timedelta],
    cause: BlockCause,
    reason: str,
    step: str = _DEFAULT_REPAIR_FAILURE_STEP,
) -> RecoveryOutcome:
    # ``project.blocked_by`` is never rewritten here. It holds the short
    # original reason (set by the runner, e.g. "repeated failure: ...", or
    # typed by a human directly on the card) that ``reason_text`` above was
    # built from. A previous version of this function wrote the synthesized,
    # human-facing ``reason`` (e.g. "blocked reason (...) does not match a
    # known auto-recoverable pattern; needs human triage") back into
    # ``blocked_by``. Because the *next* recovery pass reclassifies using
    # ``blocked_by`` as its input, that synthesized text became the new
    # ``reason_text`` and got wrapped again - producing unboundedly nested
    # "blocked reason (blocked reason (...) ...) ..." text across ticks, and
    # also meant a human shortening the reason directly on the card would be
    # re-wrapped on the very next pass. Leaving ``blocked_by`` untouched
    # keeps both the Trello field and every derived ``reason`` stable (and
    # therefore Slack-deduplicable, see daemon._run_recovery_pass) across
    # any number of recovery ticks. The full human-facing diagnosis lives
    # only in the returned outcome / ``human_notified_reason`` (rendered
    # into the *visible* Trello banner, not into ``blocked_by``/PM-DATA).
    project.transition_to(ProjectStatus.BLOCKED)
    project.review_at = (now + backoff(project.recovery_attempts + 1)).isoformat()
    return RecoveryOutcome(
        project_name=project.name, trello_card_id=project.trello_card_id,
        cause=cause, action="human_required", reason=reason,
        attempts=project.recovery_attempts, step=step,
    )


def recover_project(
    project: ProjectRecord,
    now: datetime,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff: Callable[[int], timedelta] = default_backoff,
) -> Optional[RecoveryOutcome]:
    """Revisit a single project if it is currently blocked and due for a
    recheck. Returns ``None`` for a project that is not blocked at all
    (nothing to do); a ``"deferred"`` outcome when it is blocked but its
    ``review_at`` backoff has not elapsed yet; otherwise the result of
    classifying and, where safe, repairing and requeuing it.

    Mutates ``project`` in place - callers are responsible for persisting
    it (see ``daemon.run_tick``)."""
    if not project.is_blocked:
        return None
    if not _is_due(project, now):
        return RecoveryOutcome(
            project_name=project.name, trello_card_id=project.trello_card_id,
            cause=None, action="deferred",
            reason="review_at not reached yet", attempts=project.recovery_attempts,
        )

    # Self-heal any pre-existing nested-wrapper corruption (see
    # _degarble_legacy_reason) before it feeds into classification below -
    # a no-op for the normal, never-corrupted case.
    project.blocked_by = _degarble_legacy_reason(project.blocked_by)
    project.stop_reason = _degarble_legacy_reason(project.stop_reason)

    # blocked_by and stop_reason are frequently set to the exact same text
    # (the same failure recorded from two angles) - dict.fromkeys collapses
    # that exact duplicate instead of joining "X X" into reason_text, while
    # still preserving order and keeping both when they genuinely differ.
    reason_text = " ".join(
        dict.fromkeys(part for part in (project.blocked_by, project.stop_reason) if part)
    )

    # The loop guard: once this many auto-recovery cycles have been spent
    # without the project staying unblocked, stop attempting more and
    # force a human-required state - never retry forever.
    if project.recovery_attempts >= max_attempts:
        reason = (
            f"automatic recovery exhausted after {project.recovery_attempts} attempt(s) without "
            f"staying unblocked; last known block: {reason_text or 'unknown'} — needs a human"
        )
        return _mark_human_required(project, now, backoff, BlockCause.HUMAN_REQUIRED, reason, step=_EXHAUSTED_ATTEMPTS_STEP)

    if reason_text and _HUMAN_REQUIRED_PATTERNS.search(reason_text):
        # Show the short original reason as-is - no prefix/wrapper. The
        # "why" (credentials/approval needed) is already conveyed by
        # ``_HUMAN_PATTERN_STEP``, and an unwrapped reason stays byte-for-
        # byte stable across ticks (see _mark_human_required).
        return _mark_human_required(project, now, backoff, BlockCause.HUMAN_REQUIRED, reason_text, step=_HUMAN_PATTERN_STEP)

    cause = _classify_recoverable_cause(project, reason_text)
    if cause is None:
        # Same principle: the short original reason, unwrapped.
        # ``_UNKNOWN_CAUSE_STEP`` already tells the human that it matched no
        # known auto-recoverable pattern - the reason itself does not need
        # to repeat that as prose around it.
        reason = reason_text or "unspecified"
        return _mark_human_required(project, now, backoff, BlockCause.HUMAN_REQUIRED, reason, step=_UNKNOWN_CAUSE_STEP)

    repaired, note = _attempt_repair(project, cause)
    if not repaired:
        step = _REPAIR_FAILURE_STEPS.get(cause, _DEFAULT_REPAIR_FAILURE_STEP)
        return _mark_human_required(project, now, backoff, cause, note, step=step)

    project.recovery_attempts += 1
    project.blocked_by = None
    # Recovery only makes the card eligible again.  The PM takes work from
    # Připraveno and moves it to Pracuje se only after acquiring its lock.
    project.transition_to(ProjectStatus.READY)
    project.stop_reason = f"auto-recovery ({cause.value}): {note}"
    project.review_at = None
    return RecoveryOutcome(
        project_name=project.name, trello_card_id=project.trello_card_id,
        cause=cause, action="requeued", reason=project.stop_reason,
        attempts=project.recovery_attempts,
    )


def scan_for_recovery(
    projects: list[ProjectRecord],
    now: datetime,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff: Callable[[int], timedelta] = default_backoff,
) -> list[RecoveryOutcome]:
    """Revisit every currently-blocked project in ``projects``, in place.
    Non-blocked projects are skipped entirely (no outcome recorded)."""
    outcomes = []
    for project in projects:
        outcome = recover_project(project, now, max_attempts=max_attempts, backoff=backoff)
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes
