import pytest

from ai_project_manager.card_contract import (
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

    assert fields == ["open_feedback", "lifecycle_status", "dod_routing_policy"]
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


@pytest.mark.parametrize(
    "raw, error_type",
    [
        ({"schema_version": 1, "open_feedback": "not-a-list"}, CardContractError),
        ({"schema_version": 2}, UnsupportedCardSchemaError),
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
