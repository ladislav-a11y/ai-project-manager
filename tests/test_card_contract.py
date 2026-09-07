from copy import deepcopy
import json

import pytest

from ai_project_manager.card_contract import (
    CURRENT_SCHEMA_VERSION,
    DOD_ROUTING_POLICY,
    GOVERNANCE_POLICY,
    KNOWN_FIELDS,
    CardContractError,
    UnsupportedCardSchemaError,
    dod_contract_issues,
    migrate_and_validate,
    repair_incomplete_contract,
)


def test_repair_incomplete_contract_adds_only_safe_missing_fields():
    raw = {
        "schema_version": 1,
        "main_task": "Zachovat zadání",
        "checkpoint": {"run_id": "keep-me"},
        "dod": [{"text": "test", "checked": False}],
        "governance": {
            "source_of_truth": "trello",
            "control_hierarchy": ["ai-project-manager", "ai-orchestrator", "agents"],
            "audit_authority": "ai-orchestrator",
        },
    }

    repaired, fields = repair_incomplete_contract(raw)

    assert fields == ["open_feedback", "lifecycle_status", "dod_routing_policy", "schema_version"]
    assert repaired["open_feedback"] == []
    assert repaired["lifecycle_status"] is None
    assert repaired["checkpoint"] == raw["checkpoint"]
    assert repaired["dod"] == raw["dod"]
    assert repaired["main_task"] == raw["main_task"]


def test_repair_incomplete_contract_migrates_unversioned_card():
    repaired, fields = repair_incomplete_contract({"main_task": "legacy"})

    migrated = migrate_and_validate({"main_task": "legacy"})
    assert {key: repaired[key] for key in migrated} == migrated
    assert fields == [
        "schema_version",
        "checkpoint",
        "dod",
        "open_feedback",
        "lifecycle_status",
        "governance",
        "dod_routing_policy",
    ]


@pytest.mark.parametrize("source_version", [0, 1])
def test_every_compatible_schema_loads_through_the_current_migration_path(source_version):
    raw = {"schema_version": source_version, "main_task": "preserve me"}

    migrated = migrate_and_validate(raw)

    assert migrated["schema_version"] == CURRENT_SCHEMA_VERSION
    assert migrated["main_task"] == "preserve me"
    assert migrated["checkpoint"] == {}
    assert migrated["dod"] == []
    assert migrated["open_feedback"] == []
    assert migrated["lifecycle_status"] is None
    assert raw == {"schema_version": source_version, "main_task": "preserve me"}


def test_current_schema_is_validated_without_a_version_rewrite():
    current = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "checkpoint": {},
        "dod": [],
        "open_feedback": [],
        "lifecycle_status": None,
    }

    assert migrate_and_validate(current)["schema_version"] == CURRENT_SCHEMA_VERSION


def test_compact_routing_policy_keeps_invariants_without_repeated_prose():
    rendered = json.dumps(DOD_ROUTING_POLICY, ensure_ascii=False, separators=(",", ":"))

    assert CURRENT_SCHEMA_VERSION == 3
    assert len(rendered) < 1400
    assert DOD_ROUTING_POLICY["implementation_owner"] == "agent"
    assert DOD_ROUTING_POLICY["audit_owner"] == "ai-orchestrator"
    assert "no new commit" in DOD_ROUTING_POLICY["audit_evidence_rule"]
    assert "P5" in DOD_ROUTING_POLICY["repair_priority_rule"]


def test_schema_v2_migrates_verbose_routing_policy_to_compact_policy():
    raw = {
        "schema_version": 2,
        "checkpoint": {},
        "dod": [],
        "open_feedback": [],
        "lifecycle_status": None,
        "governance": GOVERNANCE_POLICY,
        "dod_routing_policy": {
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
                "actionable audit rejection is persisted as feedback, materialized as one implementation rework item, and consumed by the next tick",
                "an explicitly non-rework audit gate may remain in Testování",
            ],
            "inbox_dependency_rule": (
                "AI Inbox subtasks declare zero-based depends_on indices; cycles and missing "
                "dependencies fail closed, and priority orders only dependency-ready siblings"
            ),
            "repair_priority_rule": (
                "corrective work, confirmed bugs, regressions, and rework are always P5; "
                "an explicit lower source label cannot demote them at Inbox intake; "
                "after a card enters Připraveno its assigned priority is immutable and "
                "must never be recomputed from status, provider, phase, or card text"
            ),
        },
    }

    migrated = migrate_and_validate(raw)

    assert migrated["schema_version"] == CURRENT_SCHEMA_VERSION
    assert migrated["dod_routing_policy"] == DOD_ROUTING_POLICY


@pytest.mark.parametrize(
    "raw, error_type",
    [
        ({"schema_version": 1, "open_feedback": "not-a-list"}, CardContractError),
        ({"schema_version": CURRENT_SCHEMA_VERSION + 1}, UnsupportedCardSchemaError),
        ({"schema_version": 1, "governance": {"source_of_truth": "local"}}, CardContractError),
    ],
)
def test_repair_incomplete_contract_does_not_mask_unsafe_cards(raw, error_type):
    with pytest.raises(error_type):
        repair_incomplete_contract(raw)


def test_legacy_learning_context_is_dropped_not_migrated_into_unknown_fields():
    raw = {
        "schema_version": 1,
        "main_task": "Keep working",
        "checkpoint": {},
        "dod": [],
        "open_feedback": [],
        "governance": {
            "source_of_truth": "trello",
            "control_hierarchy": ["ai-project-manager", "ai-orchestrator", "agents"],
            "audit_authority": "ai-orchestrator",
        },
        "learning_context": {
            "version": 1,
            "current_strategy": "stale cross-card strategy",
            "history": [{"phase": "audit", "result": "rejected"}],
        },
    }

    migrated = migrate_and_validate(raw)

    assert "learning_context" not in migrated
    assert "learning_context" not in KNOWN_FIELDS

    repaired, fields = repair_incomplete_contract(raw)
    assert "learning_context" not in repaired
    assert "learning_context" not in fields


def test_previous_routing_contract_migrates_to_current_contract():
    legacy = {
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
    migrated = migrate_and_validate({
        "schema_version": 1,
        "checkpoint": {},
        "dod": [],
        "open_feedback": [],
        "lifecycle_status": None,
        "dod_routing_policy": legacy,
    })
    assert migrated["dod_routing_policy"]["commit_rule"].startswith("agents never commit")


def test_terminal_live_contract_without_test_execution_rule_migrates():
    legacy = deepcopy(DOD_ROUTING_POLICY)
    legacy.pop("test_execution_rule")
    legacy["dispatch_requirements"] = [
        "at least one implementation DoD item remains for Pracuje se",
        "audit-only work is routed to Testování",
        "audit rejection is persisted as feedback and consumed by the next tick",
    ]
    migrated = migrate_and_validate({
        "schema_version": 1,
        "checkpoint": {},
        "dod": [],
        "open_feedback": [],
        "lifecycle_status": "done",
        "governance": GOVERNANCE_POLICY,
        "dod_routing_policy": legacy,
    })
    assert migrated["dod_routing_policy"] == DOD_ROUTING_POLICY


def test_dod_contract_rejects_controller_verification_in_implementation_phase():
    blocked = {
        "text": (
            "ai-orchestrator musí provést syntaxe -> cílené testy -> "
            "git --no-pager diff -> git --no-pager diff --check -> plný test suite"
        ),
        "phase": "implementation",
    }
    assert "controller-only verification" in dod_contract_issues([blocked])[0]

    audit = {
        "text": (
            "accepted / rejected: ai-orchestrator audit ověří syntaxe -> cílené testy -> "
            "git --no-pager diff -> git --no-pager diff --check -> plný test suite; "
            "nový commit není podmínkou"
        ),
        "phase": "audit",
    }
    assert dod_contract_issues([audit]) == []


def test_dod_contract_requires_no_commit_wording_for_existing_state_audit():
    ambiguous = {
        "text": (
            "accepted / rejected: ai-orchestrator audit ověří HEAD/status/diff/remote/push "
            "proti realitě"
        ),
        "phase": "audit",
    }
    issues = dod_contract_issues([ambiguous])
    assert "new commit is not required" in issues[0]

    explicit = {
        "text": (
            "accepted / rejected: ai-orchestrator audit musí ověřit HEAD/status/diff/remote/push "
            "proti realitě; nový commit není podmínkou"
        ),
        "phase": "audit",
    }
    assert dod_contract_issues([explicit]) == []


def test_post_done_finalization_policy_requires_explicit_human_approval():
    valid = {
        "schema_version": 1,
        "checkpoint": {},
        "dod": [],
        "open_feedback": [],
        "governance": GOVERNANCE_POLICY,
        "dod_routing_policy": DOD_ROUTING_POLICY,
        "completion_policy": {
            "mode": "post_done_finalization",
            "human_approved": True,
            "controller_owner": "ai-orchestrator",
        },
    }
    assert migrate_and_validate(valid)["completion_policy"]["mode"] == "post_done_finalization"

    invalid = dict(valid)
    invalid["completion_policy"] = {
        "mode": "post_done_finalization",
        "human_approved": False,
        "controller_owner": "ai-orchestrator",
    }
    with pytest.raises(CardContractError, match="human_approved"):
        migrate_and_validate(invalid)


@pytest.mark.parametrize(
    "text",
    [
        "spustit regresní testy enabled/disabled/failure/success",
        "spustit plný test suite",
        "kompletní testy projdou",
    ],
)
def test_dod_contract_routes_orchestrator_test_execution_to_audit(text):
    issues = dod_contract_issues([{"text": text, "phase": "implementation"}])

    assert "requests test execution in implementation" in issues[0]


def test_dod_contract_allows_orchestrator_test_execution_as_audit():
    audit = {
        "text": (
            "ai-orchestrator audit ověří spustit plný test suite a regresní testy; "
            "nový commit není podmínkou"
        ),
        "phase": "audit",
    }

    assert dod_contract_issues([audit]) == []
