"""Locator ladder, policy guardrails, redaction, lease state machine — all pure, no browser."""
import json

import pytest

from agent_hands.evidence import EvidenceLog
from agent_hands.handoff import ControlLease, LeaseState, NotLeaseHolder
from agent_hands.locator import Ambiguous, NotFound, anchor_from_element, resolve
from agent_hands.policy import Policy, PolicyViolation
from agent_hands.schema import ActionType, Anchor, RiskClass, TableCell
from agent_hands.surface.base import Element, Observation, TableInfo


def obs(*els):
    return Observation(elements=list(els), visible_text=" ".join(e.name for e in els), location="http://x/")


E = Element


class TestLocatorLadder:
    def test_exact_role_name_wins(self):
        o = obs(E("a", "button", "Search"), E("b", "button", "Reset"))
        r = resolve(Anchor(anchor_id="x", role="button", name="search"), o)
        assert r.element.ref == "a" and r.rung == "exact" and not r.drifted

    def test_button_and_link_are_interchangeable(self):
        o = obs(E("a", "link", "Search"))
        assert resolve(Anchor(anchor_id="x", role="button", name="Search"), o).element.ref == "a"

    def test_label_rung_for_legacy_table_forms(self):
        o = obs(E("a", "textbox", "", label="Member Number"), E("b", "textbox", "", label="Password"))
        r = resolve(Anchor(anchor_id="x", role="textbox", label="Member Number:"), o)
        assert r.element.ref == "a" and r.rung == "label"

    def test_table_cell_by_headers_not_indexes(self):
        o = obs(E("a", "cell", "$1.00", table=TableInfo("Checking", "Account Type", "Current Balance")),
                E("b", "cell", "$2.00", table=TableInfo("Regular Savings", "Account Type", "Current Balance")))
        a = Anchor(anchor_id="x", role="cell", table_cell=TableCell(row_header="Regular Savings", column_header="Current Balance"))
        r = resolve(a, o)
        assert r.element.ref == "b" and r.rung == "table"

    def test_fuzzy_rung_is_flagged_as_drift(self):
        o = obs(E("a", "button", "Search Members"))
        r = resolve(Anchor(anchor_id="x", role="button", name="Search"), o)
        assert r.element.ref == "a" and r.rung == "fuzzy" and r.drifted

    def test_frame_path_restricts_candidates(self):
        o = obs(E("a", "link", "Home", frame=["menu"]), E("b", "link", "Home", frame=["main"]))
        assert resolve(Anchor(anchor_id="x", role="link", name="Home", frame=["main"]), o).element.ref == "b"

    def test_ambiguity_is_an_error_not_a_guess(self):
        o = obs(E("a", "button", "Confirm", bbox=(0, 0, 10, 10)), E("b", "button", "Confirm", bbox=(500, 500, 10, 10)))
        with pytest.raises(Ambiguous):
            resolve(Anchor(anchor_id="x", role="button", name="Confirm"), o)

    def test_ambiguity_resolved_by_recorded_geometry(self):
        o = obs(E("a", "button", "Confirm", bbox=(0, 0, 10, 10)), E("b", "button", "Confirm", bbox=(500, 500, 10, 10)))
        r = resolve(Anchor(anchor_id="x", role="button", name="Confirm", bbox=[498, 498, 12, 12]), o)
        assert r.element.ref == "b" and r.rung == "exact"

    def test_near_text_rung(self):
        o = obs(E("t", "cell", "Initial Deposit", bbox=(0, 100, 80, 20)), E("a", "textbox", "", bbox=(100, 100, 80, 20)),
                E("z", "textbox", "", bbox=(100, 400, 80, 20)))
        r = resolve(Anchor(anchor_id="x", role="textbox", near_text="Initial Deposit"), o)
        assert r.element.ref == "a" and r.rung == "near"

    def test_geometric_fallback_is_opt_in(self):
        o = obs(E("a", "button", "Renamed", bbox=(10, 10, 50, 20)))
        with pytest.raises(NotFound):
            resolve(Anchor(anchor_id="x", role="button", name="Search", bbox=[10, 10, 50, 20]), o)
        r = resolve(Anchor(anchor_id="x", role="button", name="Search", bbox=[10, 10, 50, 20], allow_geometric_fallback=True), o)
        assert r.rung == "geometric" and r.element.ref == "a"

    def test_disabled_controls_are_never_targets(self):
        o = obs(E("a", "button", "Search", disabled=True))
        with pytest.raises(NotFound):
            resolve(Anchor(anchor_id="x", role="button", name="Search"), o)

    def test_anchor_from_element_captures_headers_and_frame(self):
        e = E("a", "cell", "$2.00", frame=["main"], table=TableInfo("Regular Savings", "Account Type", "Current Balance"))
        a = anchor_from_element("bal", e)
        assert a.table_cell.row_header == "Regular Savings" and a.frame == ["main"]


class TestPolicy:
    def setup_method(self):
        self.p = Policy.for_local_target("127.0.0.1:5055")

    def test_host_allowlist(self):
        assert self.p.host_allowed("http://127.0.0.1:5055/t/meridian/")
        assert not self.p.host_allowed("http://evil.example/t/meridian/")
        with pytest.raises(PolicyViolation, match="HOST_NOT_ALLOWED"):
            self.p.check(ActionType.NAVIGATE, url="http://evil.example/")

    def test_route_denylist_beats_prefix(self):
        with pytest.raises(PolicyViolation, match="ROUTE_NOT_ALLOWED"):
            self.p.check(ActionType.NAVIGATE, url="http://127.0.0.1:5055/t/meridian/logout")
        with pytest.raises(PolicyViolation, match="ROUTE_NOT_ALLOWED"):
            self.p.check(ActionType.NAVIGATE, url="http://127.0.0.1:5055/__chaos")

    def test_risk_is_raised_by_control_name_never_lowered(self):
        assert self.p.classify_control("Confirm & Open", RiskClass.REVERSIBLE) == RiskClass.IRREVERSIBLE
        assert self.p.classify_control("Search", RiskClass.REVERSIBLE) == RiskClass.REVERSIBLE
        assert self.p.classify_control("Search", RiskClass.IRREVERSIBLE) == RiskClass.IRREVERSIBLE

    def test_irreversible_requires_human_unless_approved(self):
        with pytest.raises(PolicyViolation, match="HUMAN_APPROVAL_REQUIRED"):
            self.p.check(ActionType.CLICK, control_name="Finalize", declared_risk=RiskClass.REVERSIBLE)
        assert self.p.check(ActionType.CLICK, control_name="Finalize", declared_risk=RiskClass.REVERSIBLE,
                            human_approved=True) == RiskClass.IRREVERSIBLE

    def test_block_mode(self):
        p = Policy.for_local_target("127.0.0.1:5055")
        p.risk_handling["irreversible"] = "block"
        with pytest.raises(PolicyViolation, match="RISK_BLOCKED"):
            p.check(ActionType.CLICK, control_name="Post Transaction", human_approved=True)

    def test_action_allowlist(self):
        p = Policy.for_local_target("127.0.0.1:5055")
        p.allowed_actions = ["click", "read"]
        with pytest.raises(PolicyViolation, match="ACTION_NOT_ALLOWED"):
            p.check(ActionType.TYPE)


class TestRedaction:
    def test_values_and_patterns(self):
        r = Policy().redactor({"secret:PASS": "demo-pass-123", "member_ssn": "123-45-6789"})
        out = r("pwd demo-pass-123 ssn 123-45-6789 card 4111 1111 1111 1111 mail a@b.co acct S-0012345-01 token=abc")
        assert "demo-pass-123" not in out and "6789" not in out and "4111" not in out and "a@b.co" not in out
        assert "S-0012345-01" not in out and "token=abc" not in out
        assert "[REDACTED secret:PASS]" in out

    def test_evidence_log_never_persists_secrets(self, tmp_path):
        r = Policy().redactor({"secret:PASS": "demo-pass-123"})
        log = EvidenceLog.start(tmp_path, "test", r, meta={"note": "typed demo-pass-123"})
        log.event("x", nested={"v": "demo-pass-123", "list": ["demo-pass-123"]})
        log.finish({"status": "ok", "observed": "demo-pass-123"})
        blob = (log.root / "run.jsonl").read_text() + (log.root / "result.json").read_text()
        assert "demo-pass-123" not in blob
        assert json.loads((log.root / "result.json").read_text())["status"] == "ok"


class TestControlLease:
    def _log(self, tmp_path):
        return EvidenceLog.start(tmp_path, "test", Policy().redactor())

    def test_happy_path_and_who_is_in_control(self, tmp_path):
        log = self._log(tmp_path)
        lease = ControlLease(log)
        lease.assert_holder("automation")
        lease.request_intervention("stuck")
        lease.take("isaac")
        with pytest.raises(NotLeaseHolder):
            lease.assert_holder("automation")
        lease.assert_holder("human:isaac")
        lease.release("isaac", "done")
        assert lease.state == LeaseState.AUTOMATION and lease.holder == "automation"
        transfers = [json.loads(l) for l in (log.root / "run.jsonl").read_text().splitlines() if '"control.transfer"' in l]
        assert [t["to"] for t in transfers] == ["automation(paused)", "human:isaac", "automation(resuming)", "automation"]

    def test_illegal_transitions(self, tmp_path):
        lease = ControlLease(self._log(tmp_path))
        with pytest.raises(RuntimeError, match="illegal"):
            lease.take("isaac")          # nobody asked
        lease.request_intervention("x")
        lease.take("isaac")
        lease.abort("human:isaac", "no")
        with pytest.raises(RuntimeError, match="illegal"):
            lease.release("isaac")       # aborted is terminal
