from datetime import datetime, timedelta, timezone

from ai_project_manager.models import ProjectRecord, ProjectStatus
from ai_project_manager.recovery import (
    BlockCause,
    DEFAULT_MAX_ATTEMPTS,
    _degarble_legacy_reason,
    default_backoff,
    recover_project,
    scan_for_recovery,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _blocked(**overrides) -> ProjectRecord:
    defaults = dict(
        name="Widget",
        priority=4,
        status=ProjectStatus.BLOCKED,
        main_task="Build the widget",
        orchestrator_ready_task="Implement the widget end to end",
        checkpoint={"step": 3},
        blocked_by="connection reset while calling the provider",
    )
    defaults.update(overrides)
    return ProjectRecord(**defaults)


# ---- not blocked at all -----------------------------------------------


def test_non_blocked_project_is_skipped_entirely():
    project = ProjectRecord(name="Fine", status=ProjectStatus.IN_PROGRESS)

    assert recover_project(project, NOW) is None


def test_done_project_with_stale_blocked_by_is_not_recovery_candidate():
    # A card already moved to Hotovo (Done) must never be reconsidered by
    # recovery just because it still carries a leftover blocked_by from
    # before it was moved - the physical list wins.
    project = ProjectRecord(
        name="Finished",
        status=ProjectStatus.DONE,
        blocked_by="connection reset while calling the provider",
    )

    assert project.is_blocked is False
    assert recover_project(project, NOW) is None


# ---- review_at gating (backoff) ----------------------------------------


def test_project_not_yet_due_is_deferred_and_left_untouched():
    project = _blocked(review_at=(NOW + timedelta(minutes=5)).isoformat())

    outcome = recover_project(project, NOW)

    assert outcome.action == "deferred"
    assert project.status == ProjectStatus.BLOCKED
    assert project.blocked_by == "connection reset while calling the provider"


def test_project_past_its_review_at_is_reconsidered():
    project = _blocked(review_at=(NOW - timedelta(minutes=1)).isoformat())

    outcome = recover_project(project, NOW)

    assert outcome.action == "requeued"


def test_non_string_review_at_from_corrupt_external_data_is_treated_as_due():
    project = _blocked(review_at=123)  # type: ignore[arg-type]

    outcome = recover_project(project, NOW)

    assert outcome.action == "requeued"


# ---- classification: provider/protocol error -> requeue ----------------


def test_provider_protocol_error_is_requeued_preserving_priority_and_checkpoint():
    project = _blocked(blocked_by="connection reset while calling the provider")

    outcome = recover_project(project, NOW)

    assert outcome.action == "requeued"
    assert outcome.cause == BlockCause.PROVIDER_ERROR_RESOLVED
    assert project.is_blocked is False
    assert project.status == ProjectStatus.READY
    assert project.blocked_by is None
    assert project.priority == 4
    assert project.checkpoint == {"step": 3}
    assert project.recovery_attempts == 1
    assert project.review_at is None


# ---- classification: transient external -> requeue ----------------------


def test_transient_external_dependency_is_requeued():
    project = _blocked(blocked_by="čeká na externí systém, blokováno externí závislostí")

    outcome = recover_project(project, NOW)

    assert outcome.action == "requeued"
    assert outcome.cause == BlockCause.TRANSIENT_EXTERNAL
    assert project.status == ProjectStatus.READY


# ---- classification: missing/inconsistent PM-DATA -> safe auto-repair --


def test_missing_orchestrator_ready_task_is_derived_from_main_task_and_requeued():
    project = _blocked(
        main_task="Build the widget end to end",
        orchestrator_ready_task="",
        blocked_by="orchestrator_ready_task missing after checkpoint resume",
    )

    outcome = recover_project(project, NOW)

    assert outcome.action == "requeued"
    assert outcome.cause == BlockCause.MISSING_OR_INCONSISTENT_DATA
    assert project.orchestrator_ready_task == "Build the widget end to end"
    assert project.status == ProjectStatus.READY
    assert project.blocked_by is None


def test_missing_orchestrator_ready_task_falls_back_to_next_step():
    project = _blocked(
        main_task="",
        orchestrator_ready_task="",
        next_step="Wire up the retry button",
        blocked_by="no ready task on card",
    )

    outcome = recover_project(project, NOW)

    assert outcome.action == "requeued"
    assert project.orchestrator_ready_task == "Wire up the retry button"


def test_completely_empty_task_everywhere_needs_human_reformulation():
    # Nothing anywhere on the card to safely derive from - this is a bad/
    # incomplete spec, not a repairable metadata inconsistency.
    project = _blocked(
        main_task="",
        orchestrator_ready_task="",
        next_step="",
        open_feedback=[],
        blocked_by="no ready task on card",
    )

    outcome = recover_project(project, NOW)

    assert outcome.action == "human_required"
    assert outcome.cause == BlockCause.BAD_TASK_SPEC
    assert "reformulate" in outcome.reason
    # blocked_by keeps the short original reason - the diagnosis lives only
    # in outcome.reason/human_notified_reason, never wrapped back into it.
    assert project.blocked_by == "no ready task on card"
    assert project.status == ProjectStatus.BLOCKED
    assert project.review_at is not None


# ---- classification: bad/incomplete task spec -> human reformulation ---


def test_placeholder_task_text_everywhere_needs_human_reformulation():
    project = _blocked(
        main_task="TBD",
        orchestrator_ready_task="???",
        next_step="",
        open_feedback=[],
        blocked_by="spec looked incomplete",
    )

    outcome = recover_project(project, NOW)

    assert outcome.action == "human_required"
    assert outcome.cause == BlockCause.BAD_TASK_SPEC
    assert "reformulate" in outcome.reason
    assert project.blocked_by == "spec looked incomplete"
    # The card is left BLOCKED, never silently requeued with junk content.
    assert project.status == ProjectStatus.BLOCKED
    assert project.orchestrator_ready_task == "???"


# ---- classification: explicit human-required signal --------------------


def test_credentials_block_goes_straight_to_human_required_never_repaired():
    project = _blocked(blocked_by="missing API key credentials for the target repo")

    outcome = recover_project(project, NOW)

    assert outcome.action == "human_required"
    assert outcome.cause == BlockCause.HUMAN_REQUIRED
    assert "credential" in outcome.reason.lower() or "api" in outcome.reason.lower()
    assert project.status == ProjectStatus.BLOCKED
    # The short original reason is preserved verbatim in Trello - never
    # rewrapped with diagnostic prose.
    assert project.blocked_by == "missing API key credentials for the target repo"
    assert outcome.reason == project.blocked_by


def test_repeated_human_required_review_is_idempotent_across_many_ticks():
    """Regression for the nested-wrapper bug: a card that stays blocked for
    the same reason across many recovery ticks must keep both blocked_by
    (Trello) and outcome.reason (Slack) byte-for-byte stable - never grow a
    "blocked reason (blocked reason (...) ...) ..." nest, and never produce
    a "new" reason each tick that would defeat Slack dedup."""
    project = _blocked(blocked_by="missing API key credentials for the target repo")
    original_reason = project.blocked_by

    now = NOW
    outcomes = []
    for _ in range(5):  # several ticks past the 3-tick minimum
        outcome = recover_project(project, now)
        outcomes.append(outcome)
        assert project.blocked_by == original_reason
        assert outcome.reason == original_reason
        now += timedelta(hours=24)

    assert all(o.action == "human_required" for o in outcomes)
    # Every tick produced the exact same reason text - the precondition for
    # daemon._run_recovery_pass's Slack dedup to suppress repeats.
    assert len({o.reason for o in outcomes}) == 1


def test_unrecognized_block_reason_defaults_to_human_required_not_silent_retry():
    project = _blocked(blocked_by="something bespoke that matches no known pattern at all")

    outcome = recover_project(project, NOW)

    assert outcome.action == "human_required"
    assert outcome.cause == BlockCause.HUMAN_REQUIRED
    # The reason is the short original text, unwrapped; the "why" lives in
    # the human-facing step instead.
    assert outcome.reason == "something bespoke that matches no known pattern at all"
    assert project.blocked_by == outcome.reason
    assert "neodpovídá" in outcome.step or "does not match" in outcome.step.lower()


def test_unrecognized_block_reason_stays_unwrapped_across_many_ticks():
    """Same nested-wrapper regression as above, for the "unknown cause"
    path specifically - the one the live incident report traced the bug
    to (a card's blocked_by kept re-growing "blocked reason (...) does not
    match..." text on every recovery pass)."""
    project = _blocked(blocked_by="something bespoke that matches no known pattern at all")
    original_reason = project.blocked_by

    now = NOW
    for _ in range(5):
        outcome = recover_project(project, now)
        assert outcome.action == "human_required"
        assert project.blocked_by == original_reason
        assert outcome.reason == original_reason
        assert "blocked reason (" not in (project.blocked_by or "")
        now += timedelta(hours=24)


# ---- self-heal pre-existing (legacy) nested-wrapper corruption ----------
#
# Live-verified on the real Trello board: several cards that stayed
# blocked across many recovery ticks *before* the blocked_by-mutation fix
# had degraded to megabyte-scale "blocked reason (blocked reason (...)
# does not match...) does not match..." text. The fix above stops any
# *new* growth, but those already-corrupted cards need to self-heal on
# their own next recovery pass rather than staying garbled forever.


def _simulate_legacy_wrap(reason_text: str) -> str:
    """Reproduce exactly what the old, buggy _mark_human_required used to
    write into blocked_by for the "unknown cause" path, so tests exercise
    the real historical corruption shape instead of a hand-picked string."""
    return f"blocked reason ({reason_text}) does not match a known auto-recoverable pattern; needs human triage"


def test_degarble_leaves_clean_text_untouched():
    assert _degarble_legacy_reason(None) is None
    assert _degarble_legacy_reason("") == ""
    assert _degarble_legacy_reason("missing API key credentials for prod") == (
        "missing API key credentials for prod"
    )


def test_degarble_recovers_original_sentence_from_multi_level_nesting():
    original = "Agent returned no valid JSON contract twice in a row; needs manual protocol review."
    # Reproduce three real buggy ticks: each tick's reason_text was
    # blocked_by + " " + stop_reason, and stop_reason stayed the constant
    # original sentence throughout (exactly the shape observed live).
    level1 = _simulate_legacy_wrap(f"{original} {original}")
    level2 = _simulate_legacy_wrap(f"{level1} {original}")
    level3 = _simulate_legacy_wrap(f"{level2} {original}")

    assert _degarble_legacy_reason(level3) == original


def test_degarble_recovers_bare_word_reason_from_repeated_wrapping():
    # A generic one-word signal ("blocked") with no punctuation, and a
    # stop_reason that stays constant every tick - the other real shape
    # seen live (no sentence-level periods to key off, only word-level
    # repetition from the reason_text = blocked_by + " " + stop_reason
    # join at every nesting level).
    stop_reason = "blocked"
    corrupted = stop_reason
    for _ in range(24):
        corrupted = _simulate_legacy_wrap(f"{corrupted} {stop_reason}")

    assert _degarble_legacy_reason(corrupted) == "blocked"


def test_degarble_recovers_credentials_prefix_wrapping():
    original = "missing deploy credentials for production"
    corrupted = original
    for _ in range(3):
        corrupted = f"requires a human decision/credentials: {corrupted}"

    assert _degarble_legacy_reason(corrupted) == original


def test_recover_project_self_heals_legacy_corrupted_blocked_by_on_first_tick():
    original = "Agent returned no valid JSON contract twice in a row; needs manual protocol review."
    corrupted = _simulate_legacy_wrap(f"{original} {original}")
    corrupted = _simulate_legacy_wrap(f"{corrupted} {original}")
    project = _blocked(blocked_by=corrupted, stop_reason=original)

    outcome = recover_project(project, NOW)

    assert outcome.action == "human_required"
    assert project.blocked_by == original
    assert outcome.reason == original
    assert "blocked reason (" not in project.blocked_by

    # And it stays healed and stable on every following tick.
    now = NOW
    for _ in range(3):
        now += timedelta(hours=24)
        next_outcome = recover_project(project, now)
        assert project.blocked_by == original
        assert next_outcome.reason == original


# ---- no infinite retry loop ---------------------------------------------


def test_max_attempts_is_enforced_and_never_exceeded():
    project = _blocked(blocked_by="connection reset while calling the provider")
    now = NOW

    seen_actions = []
    for _ in range(DEFAULT_MAX_ATTEMPTS + 5):
        outcome = recover_project(project, now)
        seen_actions.append(outcome.action)
        # Simulate the project immediately re-blocking with the same
        # transient signature on the very next tick (worst case for a
        # naive implementation that would otherwise retry forever).
        if outcome.action == "requeued":
            project.status = ProjectStatus.BLOCKED
            project.blocked_by = "connection reset while calling the provider"
        now = now + timedelta(hours=24)  # always past any backoff

    assert project.recovery_attempts <= DEFAULT_MAX_ATTEMPTS
    # Once exhausted, it must permanently stop requeuing and stay human_required.
    assert seen_actions[-1] == "human_required"
    assert seen_actions.count("requeued") <= DEFAULT_MAX_ATTEMPTS
    final_outcome = recover_project(project, now + timedelta(days=365))
    assert final_outcome.action == "human_required"
    assert "exhausted" in final_outcome.reason


def test_default_backoff_grows_and_is_capped():
    small = default_backoff(1)
    bigger = default_backoff(3)
    capped = default_backoff(100)

    assert small < bigger
    assert capped == default_backoff(100)
    assert capped <= timedelta(hours=6)


# ---- scan_for_recovery over a mixed project list ------------------------


# ---- physical-list precedence (legacy PAUSED "Čeká na AI" mapping) -----


def test_paused_legacy_mapped_card_with_blocked_by_is_still_a_recovery_candidate():
    # A card with no stored lifecycle_status round-trips "Čeká na AI" to
    # PAUSED, not BLOCKED (see trello_sync.project_from_card) - it must
    # still be picked up by recovery as long as blocked_by is genuinely set.
    project = _blocked(
        status=ProjectStatus.PAUSED,
        blocked_by="connection reset while calling the provider",
    )

    outcome = recover_project(project, NOW)

    assert outcome is not None
    assert outcome.action == "requeued"


def test_explicit_human_hold_on_a_paused_card_is_never_auto_unblocked():
    project = _blocked(
        status=ProjectStatus.PAUSED,
        blocked_by="missing API key credentials for the target repo",
    )

    outcome = recover_project(project, NOW)

    assert outcome.action == "human_required"
    assert project.is_blocked is True
    assert project.blocked_by == "missing API key credentials for the target repo"


def test_scan_for_recovery_only_reports_blocked_projects():
    blocked = _blocked(name="Blocked")
    fine = ProjectRecord(name="Fine", status=ProjectStatus.READY)
    deferred = _blocked(name="Deferred", review_at=(NOW + timedelta(hours=1)).isoformat())

    outcomes = scan_for_recovery([blocked, fine, deferred], NOW)

    names = {o.project_name for o in outcomes}
    assert names == {"Blocked", "Deferred"}
    actions = {o.project_name: o.action for o in outcomes}
    assert actions["Blocked"] == "requeued"
    assert actions["Deferred"] == "deferred"
