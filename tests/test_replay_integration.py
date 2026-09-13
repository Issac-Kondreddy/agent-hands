"""
Replay engine against the live legacy app: happy path, every injected
runtime condition, and every branch of the human handoff.
"""
import json

import pytest

from agent_hands.handoff import Decision, ScriptedOperator
from agent_hands.replay import ReplayEngine
from tests.fixtures import open_subaccount_capability, savings_balance_capability

pytestmark = pytest.mark.timeout(120)


def events(result):
    return [json.loads(l) for l in open(f"{result.evidence_dir}/run.jsonl")]


def types(result):
    return [e["type"] for e in events(result)]


@pytest.fixture
def engine(surface, policy, tmp_path):
    def make(operator=None):
        return ReplayEngine(surface, policy, evidence_dir=str(tmp_path), operator=operator)
    return make


@pytest.fixture
def cap(app_url):
    return savings_balance_capability(app_url + "/t/meridian/")


@pytest.fixture
def subcap(app_url):
    return open_subaccount_capability(app_url + "/t/meridian/")


class TestHappyPathAndOutcomes:
    def test_success_returns_typed_outputs_and_stable_rungs(self, engine, cap):
        r = engine().run(cap, {"member_id": "12345"})
        assert r.status == "SUCCESS"
        assert r.outputs == {"member_name": "Dana R. Whitfield", "savings_balance": "4,812.37"}
        assert r.drift == [] and all(t.ok for t in r.steps)
        assert {t.rung for t in r.steps if t.rung} <= {"exact", "label", "table"}

    def test_second_member_generalises(self, engine, cap):
        r = engine().run(cap, {"member_id": "23456"})
        assert r.status == "SUCCESS" and r.outputs["savings_balance"] == "912.00"

    def test_not_found_is_a_business_outcome_not_a_failure(self, engine, cap):
        r = engine().run(cap, {"member_id": "99999"})
        assert r.status == "BUSINESS_OUTCOME" and r.outcome_code == "MEMBER_NOT_FOUND"
        assert "99999" in r.outcome_detail and r.failed_step == "search"

    def test_access_denied_outcome(self, engine, cap):
        r = engine().run(cap, {"member_id": "77777"})
        assert (r.status, r.outcome_code) == ("BUSINESS_OUTCOME", "ACCESS_DENIED")

    def test_bad_input_rejected_before_touching_the_ui(self, engine, cap):
        with pytest.raises(ValueError, match="pattern"):
            engine().run(cap, {"member_id": "12"})

    def test_already_signed_on_skips_login_steps(self, engine, cap):
        e = engine()
        e.run(cap, {"member_id": "12345"})
        r = e.run(cap, {"member_id": "23456"})
        assert r.status == "SUCCESS"
        assert types(r).count("step.skipped") == 3


class TestRuntimeConditions:
    def test_interstitial_is_dismissed_and_flow_continues(self, engine, cap, chaos):
        chaos(interstitial_once=1)
        r = engine().run(cap, {"member_id": "12345"})
        assert r.status == "SUCCESS"
        ev = types(r)
        assert "condition.matched" in ev and "condition.dismiss" in ev

    def test_slow_page_is_waited_for_not_slept(self, engine, cap, chaos):
        chaos(slow_ms=1200)
        r = engine().run(cap, {"member_id": "12345"})
        assert r.status == "SUCCESS"

    def test_session_expiry_triggers_recovery_block_and_resumes(self, engine, cap, chaos):
        chaos(expire_session_in=3)
        r = engine().run(cap, {"member_id": "12345"})
        assert r.status == "SUCCESS" and r.outputs["savings_balance"] == "4,812.37"
        ev = types(r)
        assert "condition.recovery" in ev
        assert any("restarting from enter_member" in t.note for t in r.steps)

    def test_http_500_is_a_hard_failure_with_debug_detail(self, engine, cap):
        r = engine().run(cap, {"member_id": "50000"})
        assert (r.status, r.outcome_code) == ("FAILED", "APP_ERROR")
        assert r.failed_step is not None and "500" in r.outcome_detail

    def test_transient_500_before_first_step(self, engine, cap, chaos):
        chaos(fail_next=1)
        r = engine().run(cap, {"member_id": "12345"})
        assert (r.status, r.outcome_code) == ("FAILED", "APP_ERROR")

    def test_expectation_failure_reports_expected_vs_observed(self, engine, cap):
        broken = cap.model_copy(deep=True)
        broken.steps[3].expect.text_visible = "Something That Never Appears"
        broken.steps[3].expect.timeout_ms = 1500
        r = engine().run(broken, {"member_id": "12345"})
        assert (r.status, r.outcome_code) == ("FAILED", "EXPECTATION_FAILED")
        assert "Something That Never Appears" in r.expected and "Member Lookup" in r.observed
        assert any(f.endswith(".png") for f in __import__("os").listdir(r.evidence_dir))

    def test_validation_error_is_a_business_outcome(self, engine, subcap):
        r = engine().run(subcap, {"member_id": "12345", "nickname": "Trip", "deposit": "5"})
        assert (r.status, r.outcome_code) == ("BUSINESS_OUTCOME", "VALIDATION_ERROR")
        assert "at least $25" in r.outcome_detail


class TestHandoff:
    PARAMS = {"member_id": "12345", "nickname": "Vacation", "deposit": "100"}

    def test_irreversible_step_without_operator_stops_as_needs_human(self, engine, subcap):
        r = engine().run(subcap, self.PARAMS)
        assert (r.status, r.outcome_code) == ("NEEDS_HUMAN", "HUMAN_APPROVAL_REQUIRED")
        assert r.failed_step == "confirm" and r.interventions == 1
        req = next(e for e in events(r) if e["type"] == "intervention.requested")
        assert req["reason_code"] == "HUMAN_APPROVAL_REQUIRED" and req["screenshot"].endswith(".png")

    def test_operator_approves_and_automation_executes(self, engine, subcap):
        op = ScriptedOperator([], Decision("approve_step", "amounts reviewed"))
        r = engine(op).run(subcap, self.PARAMS)
        assert r.status == "SUCCESS" and r.outputs["new_account_number"].startswith("S-0012345-")
        ev = events(r)
        transfers = [e["to"] for e in ev if e["type"] == "control.transfer"]
        assert transfers == ["automation(paused)", "human:scripted-operator", "automation(resuming)", "automation"]
        assert op.handled[0].step_id == "confirm"

    def test_operator_performs_step_manually_then_resumes(self, engine, subcap):
        op = ScriptedOperator([("click", ("button", "Confirm & Open"))], Decision("resume", "did it myself"))
        r = engine(op).run(subcap, self.PARAMS)
        assert r.status == "SUCCESS"
        human = [e for e in events(r) if e["type"] == "human.action"]
        assert len(human) == 1 and human[0]["controller"] == "human:scripted-operator"
        assert any(t.note == "completed by human" for t in r.steps)

    def test_operator_aborts(self, engine, subcap):
        op = ScriptedOperator([], Decision("abort", "deposit looks wrong"))
        r = engine(op).run(subcap, self.PARAMS)
        assert (r.status, r.outcome_code) == ("ABORTED", "OPERATOR_ABORTED")
        assert r.outcome_detail == "deposit looks wrong"

    def test_missing_target_escalates_with_context(self, engine, cap):
        broken = cap.model_copy(deep=True)
        broken.steps[3].target.name = "Nonexistent Menu Item"
        broken.steps[3].target.frame = ["menu"]
        op = ScriptedOperator([("click", ("link", "Member Lookup"))], Decision("resume", "opened it for you"))
        r = engine(op).run(broken, {"member_id": "12345"})
        assert r.status == "SUCCESS"
        req = next(e for e in events(r) if e["type"] == "intervention.requested")
        assert req["reason_code"] == "TARGET_NOT_FOUND"

    def test_lease_holder_stamped_on_every_event(self, engine, subcap):
        op = ScriptedOperator([("click", ("button", "Confirm & Open"))], Decision("resume"))
        r = engine(op).run(subcap, self.PARAMS)
        controllers = {e["controller"] for e in events(r)}
        assert "automation" in controllers and "human:scripted-operator" in controllers


class TestEvidence:
    def test_secrets_never_reach_disk(self, engine, cap):
        r = engine().run(cap, {"member_id": "12345"})
        import os
        blob = "".join(open(os.path.join(r.evidence_dir, f)).read() for f in os.listdir(r.evidence_dir) if not f.endswith(".png"))
        assert "demo-pass-123" not in blob and "operator1" not in blob
        assert "S-0012345-01" not in blob and "[REDACTED account_no]" in blob, "account numbers on screen are PII: redacted in snapshots"

    def test_result_json_matches_returned_contract(self, engine, cap):
        r = engine().run(cap, {"member_id": "12345"})
        saved = json.load(open(f"{r.evidence_dir}/result.json"))
        assert saved["status"] == "SUCCESS" and saved["outputs"] == r.outputs
