"""Versioned contract for the PM-DATA block embedded in Trello cards."""

from __future__ import annotations

from copy import deepcopy


CURRENT_SCHEMA_VERSION = 1
GOVERNANCE_POLICY = {
    "source_of_truth": "trello",
    "control_hierarchy": ["ai-project-manager", "ai-orchestrator", "agents"],
    "audit_authority": "ai-orchestrator",
}


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
}


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
