import pytest

from ai_project_manager.card_contract import (
    KNOWN_FIELDS,
    CardContractError,
    UnsupportedCardSchemaError,
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

    assert fields == ["open_feedback", "lifecycle_status"]
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
