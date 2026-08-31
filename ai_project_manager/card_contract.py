"""Versioned contract for the PM-DATA block embedded in Trello cards."""

from __future__ import annotations

from copy import deepcopy
import re


CURRENT_SCHEMA_VERSION = 1
GOVERNANCE_POLICY = {
    "source_of_truth": "trello",
    "control_hierarchy": ["ai-project-manager", "ai-orchestrator", "agents"],
    "audit_authority": "ai-orchestrator",
}
DOD_ROUTING_POLICY = {
    "implementation_owner": "agent",
    "audit_owner": "ai-orchestrator",
    "audit_execution": "ai-orchestrator-only; agents and PM cannot issue the verdict",
    "audit_marker_required": "accepted / rejected or independent audit",
    "audit_evidence_rule": (
        "verification of existing Git/test state is phase audit, uses explicit validation wording, "
        "and does not require a new commit"
    ),
    "test_execution_rule": (
        "test execution and test-suite results are ai-orchestrator-owned audit evidence; "
        "agents are never asked to run test commands"
    ),
    "commit_rule": (
        "agents never commit; only an explicit pending controller-finalization item may invoke "
        "the controller finalizer"
    ),
    "ready_gate": "controller-only verification must not be implementation DoD",
    "ready_requirements": [
        "concrete goal and DoD prepared before queue admission",
        "every DoD item has phase implementation or audit",
        "audit items name the independent/controller audit marker",
        "existing-state Git/test verification is phase audit and explicitly no-commit",
        "controller-only verification is never implementation work",
    ],
    "dispatch_requirements": [
        "at least one implementation DoD item remains for Pracuje se",
        "audit-only work is routed to Testování",
        "audit rejection is persisted as feedback and consumed by the next tick",
    ],
}
# The first version of the routing contract was already written to live cards.
# It is a known migration source, not an invalid user-authored contract.
_LEGACY_DOD_ROUTING_POLICY_V1 = {
    "implementation_owner": "agent",
    "audit_owner": "ai-orchestrator",
    "audit_marker_required": "accepted / rejected or independent audit",
    "ready_gate": "controller-only verification must not be implementation DoD",
    "ready_requirements": [
        "concrete goal and DoD prepared before queue admission",
        "every DoD item has phase implementation or audit",
        "audit items name the independent/controller audit marker",
        "controller-only verification is never implementation work",
    ],
    "dispatch_requirements": [
        "at least one implementation DoD item remains for Pracuje se",
        "audit-only work is routed to Testování",
    ],
}
_LEGACY_DOD_ROUTING_POLICY_V2 = deepcopy(DOD_ROUTING_POLICY)
_LEGACY_DOD_ROUTING_POLICY_V2.pop("audit_execution")
_LEGACY_DOD_ROUTING_POLICY_V3 = deepcopy(DOD_ROUTING_POLICY)
_LEGACY_DOD_ROUTING_POLICY_V3.pop("test_execution_rule")


class CardContractError(ValueError):
    """The card cannot be processed safely without risking data loss."""


class UnsupportedCardSchemaError(CardContractError):
    """The card was written by a newer PM contract than this process knows."""


KNOWN_FIELDS = {
    "schema_version", "lifecycle_status", "main_task", "open_feedback",
    "next_step", "orchestrator_ready_task", "last_output", "checkpoint",
    "blocked_by", "stop_reason", "retry_after", "recovery_attempts",
    "review_at", "human_notified_reason", "human_action_step", "dod",
    "github_repo", "google_drive_ref", "provider", "status_updated_at",
    "waiting_since", "completed_at",
    "card_identity",
    "governance",
    "dod_routing_policy",
}

_VALID_DOD_PHASES = {"implementation", "audit"}
_CONTROLLER_AUDIT_DOD_RE = re.compile(
    r"accepted\s*/\s*rejected.*(?:ai[- ]orchestrator|audit)"
    r"|(?:ai[- ]orchestrator|audit).*accepted\s*/\s*rejected"
    r"|\bai[- ]orchestrator\s+audit\b"
    r"|(?:nez(?:a|\u00e1)visl\w*|independent)\s+audit",
    re.IGNORECASE,
)
_CONTROLLER_REFERENCE_RE = re.compile(
    r"\b(?:ai[- ]orchestrator|orchestrator|controller)\b", re.IGNORECASE
)
_VERIFICATION_STEP_RE = re.compile(
    r"\bsyntax\w*\b|\bcílen\w*\s+test\w*\b|\btargeted\s+test\w*\b"
    r"|\bgit\b.{0,80}\bdiff\b|\bdiff\s+--check\b"
    r"|\b(?:pln\w*|full)\s+test\w*\b",
    re.IGNORECASE,
)
_NO_COMMIT_AUDIT_RE = re.compile(
    r"(?:nov[ýy]\s+commit\s+nen[íi]\s+podm[íi]nkou|"
    r"bez\s+(?:požadavku|nutnosti)\s+na\s+nov[ýy]\s+commit|"
    r"no\s+new\s+commit\s+(?:is\s+)?required)",
    re.IGNORECASE,
)
_TEST_EXECUTION_REQUEST_RE = re.compile(
    r"(?i)(?:\b(?:spustit|spouštět|spouští|run|execute|provést|provádět)\b"
    r".{0,80}\b(?:test\w*|pytest|unittest|suite|regres\w*)\b|"
    r"\b(?:test\w*|pytest|unittest|suite)\b.{0,80}\b(?:projít|projde|projdou|passed|pass)\b)"
)


def dod_contract_issues(dod) -> list[str]:
    """Return routing errors that could deadlock an implementation run.

    ai-orchestrator receives DoD text, not PM's structured ``phase`` field.
    Therefore a controller-owned verification item must be explicitly
    recognizable as an audit item in its text.  Otherwise the executor is
    asked to complete work that its own runtime contract forbids it from
    performing (tests, diff checks, or the final verdict), and it can burn
    every autonomous iteration without progress.

    The check deliberately targets only this contradiction.  Ordinary
    implementation text mentioning an orchestrator remains valid.
    """
    issues: list[str] = []
    for index, item in enumerate(dod or []):
        if isinstance(item, dict):
            text = item.get("text", "")
            phase = item.get("phase", "implementation")
        else:
            text = getattr(item, "text", "")
            phase = getattr(item, "phase", "implementation")
        text = text if isinstance(text, str) else ""
        if not text.strip():
            issues.append(f"DoD[{index}] must contain a concrete requirement")
            continue
        if phase not in _VALID_DOD_PHASES:
            issues.append(
                f"DoD[{index}] has unsupported phase {phase!r}; expected 'implementation' or 'audit'"
            )
            continue

        controller_audit_text = bool(_CONTROLLER_AUDIT_DOD_RE.search(text))
        if phase == "audit" and not controller_audit_text:
            issues.append(
                f"DoD[{index}] is phase='audit' but does not identify an independent/controller audit"
            )
            continue

        # Existing-state Git/test checks are evidence collection, not a
        # request to create a commit.  Without this explicit wording the
        # downstream validator can interpret HEAD/remote/push as a runtime
        # finalization action and deadlock an otherwise completed card.
        existing_state_check = bool(
            re.search(r"\b(?:HEAD|status|diff|remote|push)\b", text, re.IGNORECASE)
        )
        if phase == "audit" and existing_state_check and not _NO_COMMIT_AUDIT_RE.search(text):
            issues.append(
                f"DoD[{index}] audit Git verification must explicitly state that a new commit is not required"
            )
            continue

        verification_steps = len(_VERIFICATION_STEP_RE.findall(text))
        controller_reference = bool(_CONTROLLER_REFERENCE_RE.search(text))
        if (
            phase == "implementation"
            and controller_reference
            and (controller_audit_text or verification_steps >= 2)
        ):
            issues.append(
                f"DoD[{index}] assigns controller-only verification to implementation; "
                "mark it phase='audit' and name the accepted/rejected or independent audit gate"
            )
            continue

        # The orchestrator deliberately runs the configured test command
        # after implementation work and the agent is explicitly forbidden
        # from running it.  Keeping a direct test-execution request in an
        # implementation batch creates a deterministic deadlock: the agent
        # must honestly leave the item open, while the orchestrator waits for
        # all implementation items before it runs the tests. Route it through
        # the independent audit instead.
        if phase == "implementation" and _TEST_EXECUTION_REQUEST_RE.search(text):
            issues.append(
                f"DoD[{index}] requests test execution in implementation; mark it phase='audit' "
                "because test commands and their results belong to ai-orchestrator"
            )
    return issues


def dispatch_contract_issues(dod) -> list[str]:
    """Return errors specific to a card entering ``Pracuje se``.

    A dispatchable card must contain implementation work.  A card whose DoD only
    asks for an audit is already past implementation and belongs in
    ``Testování``; sending it through ``Pracuje se`` would waste a provider
    run and cannot produce meaningful progress.
    """
    items = list(dod or [])
    issues = dod_contract_issues(items)
    if items and not any(
        (item.get("phase", "implementation") if isinstance(item, dict) else getattr(item, "phase", "implementation"))
        == "implementation"
        for item in items
    ):
        issues.append("Pracuje se requires at least one implementation DoD item; audit-only work belongs in Testování")
    return issues


def ready_contract_issues(dod) -> list[str]:
    """Backward-compatible alias for callers that call this a Ready gate."""
    return dispatch_contract_issues(dod)


def migrate_and_validate(raw: dict) -> dict:
    """Migrate legacy unversioned data in memory and validate current data.

    Unknown fields are deliberately retained.  A future schema is rejected
    before any write, so an older PM can never destructively downgrade it.
    """
    if not isinstance(raw, dict):
        raise CardContractError("PM-DATA must be a JSON object")
    data = deepcopy(raw)
    # Legacy cross-card AI "learning" memory is no longer part of the
    # contract; drop it here so it can never leak into extra_data or a
    # future agent prompt, instead of carrying it forward forever.
    data.pop("learning_context", None)
    version = data.get("schema_version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise CardContractError("schema_version must be a non-negative integer")
    if version > CURRENT_SCHEMA_VERSION:
        raise UnsupportedCardSchemaError(
            f"unsupported Trello Card Contract schema_version={version}; "
            f"this PM supports up to {CURRENT_SCHEMA_VERSION}"
        )
    if version == 0:
        data["schema_version"] = CURRENT_SCHEMA_VERSION
        data.setdefault("checkpoint", {})
        data.setdefault("dod", [])
        data.setdefault("open_feedback", [])
        data.setdefault("lifecycle_status", None)

    # Governance is an invariant, not card-authored configuration. Cards
    # written before this field existed are safely upgraded; a conflicting
    # declaration is rejected instead of silently changing authority.
    data.setdefault("governance", deepcopy(GOVERNANCE_POLICY))
    data.setdefault("dod_routing_policy", deepcopy(DOD_ROUTING_POLICY))
    if data.get("dod_routing_policy") in (
        _LEGACY_DOD_ROUTING_POLICY_V1,
        _LEGACY_DOD_ROUTING_POLICY_V2,
        _LEGACY_DOD_ROUTING_POLICY_V3,
    ):
        # Upgrade only this exact prior PM-authored contract. Any other
        # conflicting value remains fail-closed below.
        data["dod_routing_policy"] = deepcopy(DOD_ROUTING_POLICY)

    if data.get("schema_version") != CURRENT_SCHEMA_VERSION:
        raise CardContractError("card migration did not reach the current schema")
    if not isinstance(data.get("checkpoint"), dict):
        raise CardContractError("checkpoint must be a JSON object")
    if not isinstance(data.get("dod"), list):
        raise CardContractError("dod must be a JSON array")
    if any(not isinstance(item, dict) or not isinstance(item.get("text", ""), str) for item in data["dod"]):
        raise CardContractError("every dod item must be an object with string text")
    if not isinstance(data.get("open_feedback"), list):
        raise CardContractError("open_feedback must be a JSON array")
    lifecycle = data.get("lifecycle_status")
    if lifecycle is not None and not isinstance(lifecycle, str):
        raise CardContractError("lifecycle_status must be a string or null")
    identity = data.get("card_identity")
    if identity is not None:
        if not isinstance(identity, dict):
            raise CardContractError("card_identity must be a JSON object or null")
        if not isinstance(identity.get("card_id"), str) or not identity["card_id"]:
            raise CardContractError("card_identity.card_id must be a non-empty string")
        if not isinstance(identity.get("card_url"), str) or not identity["card_url"]:
            raise CardContractError("card_identity.card_url must be a non-empty string")
    if data.get("governance") != GOVERNANCE_POLICY:
        raise CardContractError(
            "governance must declare Trello as source of truth, hierarchy "
            "PM -> orchestrator -> agents, and ai-orchestrator as sole audit authority"
        )
    if data.get("dod_routing_policy") != DOD_ROUTING_POLICY:
        raise CardContractError(
            "dod_routing_policy must keep implementation work with the agent, "
            "audit/verdict work with ai-orchestrator, and reject controller-only "
            "verification in implementation DoD"
        )
    completion_policy = data.get("completion_policy")
    if completion_policy is not None:
        if not isinstance(completion_policy, dict):
            raise CardContractError("completion_policy must be a JSON object or null")
        if completion_policy.get("mode") not in {"post_done_finalization"}:
            raise CardContractError("completion_policy.mode is unsupported")
        if completion_policy.get("human_approved") is not True:
            raise CardContractError("post_done_finalization requires explicit human_approved=true")
        if completion_policy.get("controller_owner") != "ai-orchestrator":
            raise CardContractError("post_done_finalization must remain controller-owned by ai-orchestrator")
    return data


def repair_incomplete_contract(raw: dict) -> tuple[dict, list[str]]:
    """Repair only fields whose absence has a safe, versioned default.

    This is intentionally narrower than migration: malformed values,
    conflicting governance, identity mismatches, and future schema versions
    remain hard errors.  The returned field names are an audit trail for the
    caller, which can persist the repaired contract back to Trello and verify
    it there.  No task text, DoD item, checkpoint, or lifecycle transition is
    invented here.
    """
    if not isinstance(raw, dict):
        raise CardContractError("PM-DATA must be a JSON object")

    repaired = deepcopy(raw)
    repaired_fields: list[str] = []

    version = repaired.get("schema_version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise CardContractError("schema_version must be a non-negative integer")
    if version > CURRENT_SCHEMA_VERSION:
        raise UnsupportedCardSchemaError(
            f"unsupported Trello Card Contract schema_version={version}; "
            f"this PM supports up to {CURRENT_SCHEMA_VERSION}"
        )

    # These values are neutral containers or a value derived from the
    # physical Trello list later by project_from_card.  They never fabricate
    # work and preserve any value that the card already supplied.
    safe_defaults = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "checkpoint": {},
        "dod": [],
        "open_feedback": [],
        "lifecycle_status": None,
        "governance": deepcopy(GOVERNANCE_POLICY),
        "dod_routing_policy": deepcopy(DOD_ROUTING_POLICY),
    }
    for field, default in safe_defaults.items():
        if field not in repaired:
            repaired[field] = deepcopy(default)
            repaired_fields.append(field)

    # An explicit schema_version=0 is a legacy contract.  Normalize it to
    # the current version while retaining all user-authored fields.
    if version == 0 and repaired.get("schema_version") != CURRENT_SCHEMA_VERSION:
        repaired["schema_version"] = CURRENT_SCHEMA_VERSION
        repaired_fields.append("schema_version")

    return migrate_and_validate(repaired), repaired_fields


def unknown_fields(data: dict) -> dict:
    return {key: deepcopy(value) for key, value in data.items() if key not in KNOWN_FIELDS}
