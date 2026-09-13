"""
Discovery loop → recorder → artifact → replay, hermetically (fake model),
plus tenant overlay and catalog/approval gate.

The genuine LLM-driven run is in /evidence; these tests prove the plumbing
around it is deterministic and safe.
"""
import json
import os

import pytest

from agent_hands.catalog import Catalog
from agent_hands.discovery.agent import DiscoveryAgent
from agent_hands.discovery.recorder import AppProfile, Recorder
from agent_hands.handoff import Decision, ScriptedOperator
from agent_hands.replay import ReplayEngine
from agent_hands.schema import Capability, Parameter, TenantOverlay
from agent_hands.surface.base import Element, Observation
from agent_hands.surface.playwright_surface import PlaywrightSurface
from tests.fake_llm import FakeAnthropic, not_found_script, savings_balance_script

pytestmark = pytest.mark.timeout(180)
PROFILE = os.path.join(os.path.dirname(__file__), "..", "profiles", "meridian_core.json")
GOAL = "Look up member ${member_id} and read their current regular savings balance"
SECRETS = ["MERIDIAN_DEMO_USER", "MERIDIAN_DEMO_PASS"]


def make_agent(surface, policy, app_url, tmp_path, script, values):
    return DiscoveryAgent(surface, policy, app_id="meridian_core", entry_point=f"{app_url}/t/meridian/",
                          parameters=[Parameter(name="member_id", description="5-digit member number", pattern=r"\d{5}")],
                          param_values=values, secret_names=SECRETS, profile=AppProfile.load(PROFILE), tenant="meridian",
                          evidence_dir=str(tmp_path / "ev"), artifacts_dir=str(tmp_path / "art"), client=FakeAnthropic(script))


@pytest.fixture
def discovered(surface, policy, app_url, tmp_path):
    r = make_agent(surface, policy, app_url, tmp_path, savings_balance_script(), {"member_id": "12345"}).run(GOAL)
    assert r.status == "SUCCESS", r.detail
    return r


class TestDiscoveryToArtifact:
    def test_artifact_is_parameterised_and_secret_free(self, discovered):
        text = open(discovered.artifact_path).read()
        cap = Capability.from_json(text)
        assert "12345" not in text.replace("0012345", ""), "the example value must not be baked into the flow"
        assert "demo-pass-123" not in text and "operator1" not in text
        assert any(s.value == "${member_id}" for s in cap.steps)
        assert any(s.value == "${secret:MERIDIAN_DEMO_PASS}" for s in cap.steps)
        assert cap.status == "draft"

    def test_value_cells_are_anchored_by_label_or_headers_not_text(self, discovered):
        cap = Capability.from_json(open(discovered.artifact_path).read())
        reads = [s for s in cap.steps if s.action.value == "read"]
        assert len(reads) == 2
        for s in reads:
            assert s.target.name is None
            assert s.target.label or s.target.table_cell
        assert any(s.target.table_cell and s.target.table_cell.row_header == "Regular Savings" for s in reads)

    def test_login_steps_are_idempotent_and_profile_rules_merged(self, discovered):
        cap = Capability.from_json(open(discovered.artifact_path).read())
        assert all(s.skip_if is not None for s in cap.steps[:3])
        assert cap.steps[3].skip_if is None
        rule = next(c for c in cap.conditions if c.rule_id == "session_expired")
        assert rule.response.resume_from_step == cap.steps[3].step_id, "resume placeholder resolved to first post-sign-on step"
        assert "re_signon" in cap.recovery_blocks

    def test_expectations_were_derived_from_what_appeared(self, discovered):
        cap = Capability.from_json(open(discovered.artifact_path).read())
        search = next(s for s in cap.steps if "search" in s.step_id)
        assert search.expect and search.expect.text_visible == "Member Detail"

    def test_recorded_artifact_replays_on_a_different_member(self, discovered, surface, policy, tmp_path):
        cap = Capability.from_json(open(discovered.artifact_path).read())
        surface.reset_session()
        r = ReplayEngine(surface, policy, evidence_dir=str(tmp_path / "rep")).run(cap, {"member_id": "23456"})
        assert r.status == "SUCCESS" and r.outputs == {"member_name": "Miguel A. Sandoval", "savings_balance": "912.00"}

    def test_discovery_evidence_has_screens_and_no_secrets(self, discovered):
        files = os.listdir(discovered.evidence_dir)
        assert "step-00.png" in files and any(f.endswith(".obs.txt") for f in files)
        blob = open(f"{discovered.evidence_dir}/run.jsonl").read()
        assert "demo-pass-123" not in blob
        assert '"llm.response"' in blob and '"agent.action"' in blob

    def test_model_cannot_claim_success_it_cannot_show(self, surface, policy, app_url, tmp_path):
        script = savings_balance_script()
        bad = dict(script[-1]); bad["args"] = {**bad["args"], "checkpoint_text": "Totally Not On Screen"}
        script.insert(len(script) - 1, bad)          # first `done` lies, second quotes the screen
        r = make_agent(surface, policy, app_url, tmp_path, script, {"member_id": "12345"}).run(GOAL)
        assert r.status == "SUCCESS"
        ev = open(f"{r.evidence_dir}/run.jsonl").read()
        assert '"agent.done_rejected"' in ev and "Totally Not On Screen" in ev
        assert r.capability.checkpoint.expect.text_visible == "Current Balance"

    def test_model_that_never_shows_success_fails(self, surface, policy, app_url, tmp_path):
        script = savings_balance_script()
        script[-1]["args"]["checkpoint_text"] = "Totally Not On Screen"
        script.append({"tool": "stuck", "args": {"reason": "cannot prove it"}})
        r = make_agent(surface, policy, app_url, tmp_path, script, {"member_id": "12345"}).run(GOAL)
        assert r.status == "STUCK"


class TestEnrichment:
    def test_second_run_teaches_not_found_outcome(self, discovered, surface, policy, app_url, tmp_path):
        v1 = Capability.from_json(open(discovered.artifact_path).read())
        surface.reset_session()
        r = make_agent(surface, policy, app_url, tmp_path, not_found_script(), {"member_id": "99999"}).run(GOAL, enrich=v1)
        assert r.status == "ENRICHED"
        v2 = Capability.from_json(open(r.artifact_path).read())
        assert v2.version == 2 and [o.code for o in v2.outcomes] == ["MEMBER_NOT_FOUND"]
        assert [s.step_id for s in v2.steps] == [s.step_id for s in v1.steps], "flow untouched"
        surface.reset_session()
        rr = ReplayEngine(surface, policy, evidence_dir=str(tmp_path / "rep")).run(v2, {"member_id": "99999"})
        assert (rr.status, rr.outcome_code) == ("BUSINESS_OUTCOME", "MEMBER_NOT_FOUND")


class TestGuardrailsDuringDiscovery:
    def test_irreversible_click_refused_without_operator(self, surface, policy, app_url, tmp_path):
        script = savings_balance_script()[:6] + [
            {"tool": "click", "target": ("button", "Open Sub-Account"), "args": {"reason": "open form"}},
            {"tool": "type", "target": ("textbox", "Account Nickname"), "args": {"text": "X", "reason": "nick"}},
            {"tool": "type", "target": ("textbox", "Initial Deposit"), "args": {"text": "100", "reason": "dep"}},
            {"tool": "click", "target": ("button", "Continue"), "args": {"reason": "continue"}},
            {"tool": "click", "target": ("button", "Confirm & Open"), "args": {"reason": "confirm"}},
            {"tool": "stuck", "args": {"reason": "cannot confirm without human"}},
        ]
        r = make_agent(surface, policy, app_url, tmp_path, script, {"member_id": "12345"}).run("open a sub-account")
        assert r.status == "STUCK"
        ev = open(f"{r.evidence_dir}/run.jsonl").read()
        assert '"policy.gate"' in ev and "HUMAN_APPROVAL_REQUIRED" in ev
        # the fake model's refused tool result must say REFUSED and the confirmation screen must not have been reached
        assert "opened successfully" not in ev

    def test_navigation_outside_allowlist_is_refused(self, surface, policy, app_url, tmp_path):
        script = [{"tool": "navigate", "args": {"url": "http://evil.example/", "reason": "wander"}},
                  {"tool": "stuck", "args": {"reason": "blocked"}}]
        r = make_agent(surface, policy, app_url, tmp_path, script, {"member_id": "12345"}).run("wander")
        ev = open(f"{r.evidence_dir}/run.jsonl").read()
        assert "HOST_NOT_ALLOWED" in ev

    def test_human_actions_during_discovery_are_recorded_as_steps(self, surface, policy, app_url, tmp_path):
        script = savings_balance_script()[:4] + [{"tool": "stuck", "args": {"reason": "I do not know where to type"}}] + \
                 savings_balance_script()[5:]
        op = ScriptedOperator([("type", ("textbox", "Member Number"), "12345")], Decision("resume", "typed it"))
        ag = make_agent(surface, policy, app_url, tmp_path, script, {"member_id": "12345"})
        ag.operator = op
        r = ag.run(GOAL)
        assert r.status == "SUCCESS" and r.interventions == 1
        cap = Capability.from_json(open(r.artifact_path).read())
        human_step = next(s for s in cap.steps if s.description.startswith("(human)"))
        assert human_step.value == "${member_id}", "even a human-typed value is parameterised"


class TestRecorderUnit:
    def test_derive_expectation_prefers_new_heading_text(self):
        before = Observation([Element("a", "cell", "Member Lookup")], "", "u")
        after = Observation([Element("a", "cell", "Member Lookup"), Element("b", "cell", "Member Detail"),
                             Element("c", "cell", "12345")], "", "u")
        exp = Recorder.derive_expectation(before, after)
        assert exp.text_visible == "Member Detail"

    def test_template_substitution_secrets_first(self):
        rec = Recorder("app", "http://x/", [], {"member_id": "123"}, {"PASS": "123-secret"}, None)
        assert rec._template("123-secret") == "${secret:PASS}"
        assert rec._template("123") == "${member_id}"


class TestCatalogAndTenants:
    def test_draft_is_not_invocable_until_approved(self, discovered, tmp_path):
        cat = Catalog(tmp_path / "art", tmp_path / "ov")
        with pytest.raises(KeyError, match="approved"):
            cat.get("meridian_core.member_savings_balance")
        assert cat.list_tools() == []
        cat.approve("meridian_core.member_savings_balance", 1, "isaac")
        tools = cat.list_tools()
        assert tools[0]["name"] == "meridian_core__member_savings_balance"
        assert "member_id" in tools[0]["input_schema"]["required"]

    def test_overlay_replays_same_flow_on_second_tenant(self, discovered, surface, policy, app_url, tmp_path):
        cat = Catalog(tmp_path / "art", tmp_path / "ov")
        cat.approve("meridian_core.member_savings_balance", 1, "isaac")
        (tmp_path / "ov").mkdir()
        ov = json.load(open(os.path.join(os.path.dirname(__file__), "..", "overrides", "harbor.json")))
        ov["entry_point"] = f"{app_url}/t/harbor/"
        (tmp_path / "ov" / "harbor.json").write_text(json.dumps(ov))
        surface.reset_session()
        r = cat.invoke("meridian_core.member_savings_balance", {"member_id": "23456"}, surface=surface, policy=policy,
                       tenant="harbor", evidence_dir=str(tmp_path / "rep"))
        assert r.status == "SUCCESS" and r.outputs["savings_balance"] == "912.00" and r.drift == []

    def test_unknown_tenant_needs_an_overlay(self, discovered, policy, tmp_path):
        cat = Catalog(tmp_path / "art", tmp_path / "ov")
        cat.approve("meridian_core.member_savings_balance", 1, "isaac")
        with pytest.raises(KeyError, match="overlay"):
            cat.invoke("meridian_core.member_savings_balance", {"member_id": "1"}, surface=None, policy=policy, tenant="nowhere")
