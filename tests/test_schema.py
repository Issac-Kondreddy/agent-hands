"""Artifact schema: the contract must reject what a reviewer would reject."""
import pytest
from pydantic import ValidationError

from agent_hands.schema import (ActionType, Anchor, Capability, Checkpoint, ConditionKind, ConditionMatch,
                                ConditionResponse, ConditionRule, Expectation, Parameter, ResponseKind, Step,
                                TenantOverlay)
from tests.fixtures import open_subaccount_capability, savings_balance_capability

ENTRY = "http://127.0.0.1:1/t/meridian/"


def minimal(**over):
    base = dict(capability_id="app.thing", title="t", description="d", app_id="app", entry_point=ENTRY,
                steps=[Step(step_id="s1", action=ActionType.CLICK, target=Anchor(anchor_id="a", role="button", name="Go"))],
                checkpoint=Checkpoint(description="c", expect=Expectation(text_visible="done")))
    base.update(over)
    return Capability(**base)


def test_roundtrip_json_is_lossless():
    cap = savings_balance_capability(ENTRY)
    again = Capability.from_json(cap.to_json())
    assert again == cap
    assert again.fingerprint() == cap.fingerprint()


def test_fingerprint_changes_when_flow_changes():
    cap = savings_balance_capability(ENTRY)
    changed = cap.model_copy(update={"steps": cap.steps[:-1]})
    changed = Capability.model_validate(changed.model_dump())
    assert changed.fingerprint() != cap.fingerprint()


def test_undeclared_parameter_is_rejected():
    with pytest.raises(ValidationError, match="undeclared parameter"):
        minimal(steps=[Step(step_id="s1", action=ActionType.TYPE, value="${nope}",
                            target=Anchor(anchor_id="a", role="textbox", name="X"))])


def test_undeclared_output_is_rejected():
    with pytest.raises(ValidationError, match="undeclared output"):
        minimal(steps=[Step(step_id="s1", action=ActionType.READ, output="x",
                            target=Anchor(anchor_id="a", role="cell", label="X"))])


def test_step_shape_rules():
    with pytest.raises(ValidationError, match="requires a target"):
        Step(step_id="s", action=ActionType.CLICK)
    with pytest.raises(ValidationError, match="requires a value"):
        Step(step_id="s", action=ActionType.TYPE, target=Anchor(anchor_id="a", role="textbox", name="X"))
    with pytest.raises(ValidationError, match="requires an expectation"):
        Step(step_id="s", action=ActionType.ASSERT)


def test_anchor_needs_a_strategy():
    with pytest.raises(ValidationError, match="no locating strategy"):
        Anchor(anchor_id="empty")


def test_condition_rule_kind_response_consistency():
    ok = ConditionRule(rule_id="r", kind=ConditionKind.BUSINESS_OUTCOME, match=ConditionMatch(text_pattern="x"),
                       response=ConditionResponse(kind=ResponseKind.RETURN_OUTCOME, outcome_code="X"))
    assert ok.kind == ConditionKind.BUSINESS_OUTCOME
    with pytest.raises(ValidationError, match="business outcomes must RETURN_OUTCOME"):
        ConditionRule(rule_id="r", kind=ConditionKind.BUSINESS_OUTCOME, match=ConditionMatch(text_pattern="x"),
                      response=ConditionResponse(kind=ResponseKind.FAIL, outcome_code="X"))
    with pytest.raises(ValidationError, match="dismiss_target required"):
        ConditionRule(rule_id="r", kind=ConditionKind.RECOVERABLE, match=ConditionMatch(text_pattern="x"),
                      response=ConditionResponse(kind=ResponseKind.DISMISS))
    with pytest.raises(ValidationError, match="unknown recovery block"):
        minimal(conditions=[ConditionRule(rule_id="r", kind=ConditionKind.RECOVERABLE, match=ConditionMatch(text_pattern="x"),
                                          response=ConditionResponse(kind=ResponseKind.RUN_RECOVERY, recovery_block="nope"))])


def test_entry_point_must_not_embed_credentials():
    with pytest.raises(ValidationError, match="credentials"):
        minimal(entry_point="http://x/login?password=hunter2")


def test_max_risk_is_derived_from_steps():
    assert savings_balance_capability(ENTRY).max_risk.value == "reversible"
    assert open_subaccount_capability(ENTRY).max_risk.value == "irreversible"


def test_validate_inputs_enforces_types_and_patterns():
    cap = savings_balance_capability(ENTRY)
    assert cap.validate_inputs({"member_id": "12345"}) == {"member_id": "12345"}
    with pytest.raises(ValueError, match="pattern"):
        cap.validate_inputs({"member_id": "abc"})
    with pytest.raises(ValueError, match="missing required"):
        cap.validate_inputs({})
    with pytest.raises(ValueError, match="unknown parameter"):
        cap.validate_inputs({"member_id": "12345", "extra": "1"})
    sub = open_subaccount_capability(ENTRY)
    with pytest.raises(ValueError, match="numeric"):
        sub.validate_inputs({"member_id": "12345", "nickname": "x", "deposit": "lots"})


def test_agent_facing_contract_hides_steps_but_exposes_signature():
    c = open_subaccount_capability(ENTRY).agent_facing_contract()
    assert set(c["parameters"]) == {"member_id", "nickname", "deposit"}
    assert "new_account_number" in c["returns"]
    assert "VALIDATION_ERROR" in c["possible_outcomes"]
    assert c["requires_human_approval"] is True
    assert "steps" not in c


def test_overlay_refuses_wrong_app_and_rewrites_anchors():
    cap = savings_balance_capability(ENTRY)
    with pytest.raises(ValueError, match="cannot be applied"):
        TenantOverlay(tenant_id="t", app_id="other").apply(cap)
    ov = TenantOverlay(tenant_id="harbor", app_id="meridian_core",
                       anchors={"search_button": {"name": "Find"}}, text_substitutions={"Member Lookup": "Customer Inquiry"})
    new = ov.apply(cap)
    assert next(s for s in new.steps if s.step_id == "search").target.name == "Find"
    assert next(s for s in new.steps if s.step_id == "open_lookup").target.name == "Customer Inquiry"
    assert cap.steps[3].target.name == "Member Lookup", "base artifact must be untouched"
