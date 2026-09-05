import pytest

from ai_project_manager.providers import (
    MODEL_TIER_BALANCED,
    MODEL_TIER_ECONOMICAL,
    MODEL_TIER_QUALITY,
    ProviderRegistry,
    TASK_AUDIT,
    TASK_IMPLEMENTATION,
    TASK_INBOX_PLANNING,
)
from ai_project_manager.task_classification import (
    COMPLEXITIES,
    COMPLEXITY_HIGH,
    COMPLEXITY_LOW,
    COMPLEXITY_MEDIUM,
    classify_task,
)


def test_inbox_planning_always_uses_economical_tier_regardless_of_complexity():
    for complexity in COMPLEXITIES:
        result = classify_task(TASK_INBOX_PLANNING, complexity)
        assert result.model_tier == MODEL_TIER_ECONOMICAL


def test_audit_always_uses_quality_tier_regardless_of_complexity():
    for complexity in COMPLEXITIES:
        result = classify_task(TASK_AUDIT, complexity)
        assert result.model_tier == MODEL_TIER_QUALITY


def test_implementation_tier_scales_with_complexity():
    assert classify_task(TASK_IMPLEMENTATION, COMPLEXITY_LOW).model_tier == MODEL_TIER_ECONOMICAL
    assert classify_task(TASK_IMPLEMENTATION, COMPLEXITY_MEDIUM).model_tier == MODEL_TIER_BALANCED
    assert classify_task(TASK_IMPLEMENTATION, COMPLEXITY_HIGH).model_tier == MODEL_TIER_QUALITY


def test_inbox_planning_forbids_hermes_and_gemini_regardless_of_complexity():
    base_order = ("hermes", "antigravity", "gemini", "claude", "codex")
    for complexity in COMPLEXITIES:
        result = classify_task(TASK_INBOX_PLANNING, complexity)
        assert result.allowed_providers(base_order) == ("antigravity", "claude", "codex")


def test_implementation_and_audit_do_not_add_a_provider_restriction():
    base_order = ("hermes", "antigravity", "gemini", "claude", "codex")
    for task_type in (TASK_IMPLEMENTATION, TASK_AUDIT):
        for complexity in COMPLEXITIES:
            result = classify_task(task_type, complexity)
            assert result.allowed_providers(base_order) == base_order


def test_classify_task_rejects_unknown_task_type():
    with pytest.raises(ValueError):
        classify_task("some-other-task", COMPLEXITY_LOW)


def test_classify_task_rejects_unknown_complexity():
    with pytest.raises(ValueError):
        classify_task(TASK_IMPLEMENTATION, "extreme")


def test_model_for_tier_picks_economical_balanced_and_quality():
    registry = ProviderRegistry()
    registry.configure_models("codex", ["gpt-eco", "gpt-mid", "gpt-pro"])

    assert registry.model_for_tier("codex", MODEL_TIER_ECONOMICAL) == "gpt-eco"
    assert registry.model_for_tier("codex", MODEL_TIER_BALANCED) == "gpt-mid"
    assert registry.model_for_tier("codex", MODEL_TIER_QUALITY) == "gpt-pro"


def test_model_for_tier_balanced_falls_back_to_economical_with_fewer_than_three_models():
    registry = ProviderRegistry()
    registry.configure_models("codex", ["gpt-eco", "gpt-pro"])

    assert registry.model_for_tier("codex", MODEL_TIER_BALANCED) == "gpt-eco"


def test_model_for_tier_returns_none_for_unconfigured_or_unregistered_provider():
    registry = ProviderRegistry()
    registry.register("codex")

    assert registry.model_for_tier("codex", MODEL_TIER_ECONOMICAL) is None
    assert registry.model_for_tier("never-registered", MODEL_TIER_QUALITY) is None


def test_model_for_tier_rejects_unknown_tier():
    registry = ProviderRegistry()
    registry.configure_models("codex", ["gpt-eco", "gpt-pro"])

    with pytest.raises(ValueError):
        registry.model_for_tier("codex", "ultra")


def test_classification_end_to_end_with_registry():
    registry = ProviderRegistry()
    registry.configure_models("codex", ["gpt-eco", "gpt-mid", "gpt-pro"])

    classification = classify_task(TASK_IMPLEMENTATION, COMPLEXITY_HIGH)
    assert registry.model_for_tier("codex", classification.model_tier) == "gpt-pro"

    classification = classify_task(TASK_IMPLEMENTATION, COMPLEXITY_LOW)
    assert registry.model_for_tier("codex", classification.model_tier) == "gpt-eco"
