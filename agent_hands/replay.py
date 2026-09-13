"""
Deterministic replay — the path an AI agent triggers in production.

No model is in the loop. Given a `Capability` and typed inputs, the engine:

    for each step:
        observe → evaluate condition rules → resolve target via the locator
        ladder → policy check → act → verify the step's expectation (polling,
        never sleeping) → record rung/confidence/duration
    verify the checkpoint → return outputs

and returns a `ReplayResult` whose `status` cleanly separates:

    SUCCESS           checkpoint reached, outputs extracted
    BUSINESS_OUTCOME  a declared, legitimate non-success answer (MEMBER_NOT_FOUND …)
    FAILED            a hard failure with step / expected / observed for debugging
    NEEDS_HUMAN       we stopped and no operator was available to take over
    ABORTED           an operator took over and chose to abort

Determinism comes from: fixed step order, semantic anchors resolved by a
fixed ladder, bounded fixed-interval polling against declared expectations,
bounded rule responses (max_attempts), and zero randomness or model calls.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .evidence import EvidenceLog
from .handoff import ControlLease, Decision, InterventionRequest, LeaseState, LiveSession, Operator
from .locator import Ambiguous, LocatorError, NotFound, Resolution, resolve
from .policy import Policy, PolicyViolation
from .schema import (ActionType, Capability, ConditionKind, ConditionRule, Expectation, ResponseKind, RiskClass,
                     Step)
from .surface.base import Observation, Surface, SurfaceError

SECRET_RE = re.compile(r"\$\{secret:([A-Za-z_][A-Za-z0-9_]*)\}")
PARAM_RE = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
POLL_MS = 250


# ---------------------------------------------------------------------------
# Result contract
# ---------------------------------------------------------------------------
@dataclass
class StepTrace:
    step_id: str
    action: str
    ok: bool
    rung: Optional[str] = None
    confidence: Optional[float] = None
    attempts: int = 1
    duration_ms: int = 0
    note: str = ""


@dataclass
class ReplayResult:
    status: str                                  # SUCCESS | BUSINESS_OUTCOME | FAILED | NEEDS_HUMAN | ABORTED
    capability_id: str
    capability_version: int
    run_id: str
    evidence_dir: str
    outputs: dict[str, str] = field(default_factory=dict)
    outcome_code: Optional[str] = None           # for BUSINESS_OUTCOME / FAILED
    outcome_detail: Optional[str] = None
    failed_step: Optional[str] = None
    expected: Optional[str] = None
    observed: Optional[str] = None
    steps: list[StepTrace] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)   # step_ids that resolved below the stable rungs
    interventions: int = 0
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class _Stop(Exception):
    """Internal: carries a terminal result out of the step loop."""
    def __init__(self, status: str, code: Optional[str] = None, detail: Optional[str] = None,
                 step_id: Optional[str] = None, expected: Optional[str] = None, observed: Optional[str] = None):
        self.status, self.code, self.detail = status, code, detail
        self.step_id, self.expected, self.observed = step_id, expected, observed


class _Rerun(Exception):
    """Internal: a rule handled a condition; run the current step again."""


class _Skip(Exception):
    """Internal: a human completed the step manually; move on."""


class _Restart(Exception):
    """Internal: a recovery block reset UI state; continue from an earlier step."""
    def __init__(self, step_id: str):
        self.step_id = step_id


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class ReplayEngine:
    def __init__(self, surface: Surface, policy: Policy, evidence_dir: str = "evidence/runs",
                 operator: Optional[Operator] = None, goal: str = "",
                 secrets: Callable[[str], Optional[str]] = os.environ.get):
        self.surface, self.policy, self.operator = surface, policy, operator
        self.evidence_dir, self.goal, self._secrets = evidence_dir, goal, secrets

    # ------------------------------------------------------------ public
    def run(self, cap: Capability, params: dict, run_id: Optional[str] = None) -> ReplayResult:
        t0 = time.time()
        values = cap.validate_inputs(params)
        sensitive = {k: v for k, v in values.items() if k in cap.sensitive_param_names()}
        secret_values = {m: self._secrets(m) or "" for s in self._all_steps(cap) for m in SECRET_RE.findall(s.value or "")}
        sensitive.update({f"secret:{k}": v for k, v in secret_values.items() if v})
        redact = self.policy.redactor(sensitive)
        log = EvidenceLog.start(self.evidence_dir, "replay", redact, run_id=run_id,
                                meta={"capability_id": cap.capability_id, "version": cap.version,
                                      "params": {k: ("<REDACTED>" if k in cap.sensitive_param_names() else v)
                                                 for k, v in values.items()}})
        self._log, self._lease, self._cap, self._values = log, ControlLease(log), cap, values
        self._secret_values = secret_values
        self._outputs_store: dict[str, str] = {}
        self._rule_uses: dict[str, int] = {}   # run-level: every recoverable rule is bounded per run
        self._in_recovery = False
        result = ReplayResult(status="FAILED", capability_id=cap.capability_id, capability_version=cap.version,
                              run_id=log.run_id, evidence_dir=str(log.root))
        try:
            self._navigate(self._subst(cap.entry_point), "entry")
            i = 0
            while i < len(cap.steps):
                step = cap.steps[i]
                try:
                    trace = self._run_step_with_handling(step, result)
                    result.steps.append(trace)
                    if trace.rung and trace.rung not in {"exact", "label", "table", None}:
                        result.drift.append(step.step_id)
                    i += 1
                except _Skip:
                    result.steps.append(StepTrace(step.step_id, step.action.value, True, note="completed by human"))
                    i += 1
                except _Restart as r:
                    result.steps.append(StepTrace(step.step_id, step.action.value, False, note=f"restarting from {r.step_id} after recovery"))
                    i = next(k for k, s in enumerate(cap.steps) if s.step_id == r.step_id)
            # checkpoint — reaching the last step is not success
            obs = self._verify(cap.checkpoint.expect, "checkpoint", result)
            result.status = "SUCCESS"
            log.event("checkpoint.ok", description=cap.checkpoint.description)
            log.snapshot(self.surface, "final", obs)
        except _Stop as s:
            result.status, result.outcome_code, result.outcome_detail = s.status, s.code, s.detail
            result.failed_step, result.expected, result.observed = s.step_id, s.expected, s.observed
            try:
                log.snapshot(self.surface, "failure", self.surface.observe())
            except Exception:  # noqa: BLE001 — evidence capture must never mask the real result
                pass
        except SurfaceError as e:
            result.status, result.outcome_code, result.outcome_detail = "FAILED", "SURFACE_ERROR", str(e)
            log.snapshot(self.surface, "failure")
        result.duration_ms = int((time.time() - t0) * 1000)
        result.outputs = self._outputs
        log.finish(result.to_dict())
        return result

    # ------------------------------------------------------------ steps
    def _run_step_with_handling(self, step: Step, result: ReplayResult) -> StepTrace:
        """Runs one step, applying condition rules and escalation until it succeeds or we stop."""
        attempts = 0
        human_approved = False
        t0 = time.time()
        while True:
            attempts += 1
            if attempts > 6:
                raise _Stop("FAILED", "STEP_RETRY_EXHAUSTED", f"step {step.step_id} did not settle after {attempts-1} attempts",
                            step.step_id)
            try:
                res = self._execute(step, human_approved)
                return StepTrace(step.step_id, step.action.value, True, res.rung if res else None,
                                 res.confidence if res else None, attempts, int((time.time() - t0) * 1000))
            except _Rerun:
                continue
            except PolicyViolation as pv:
                if pv.code == "HUMAN_APPROVAL_REQUIRED":
                    decision = self._escalate(step, "HUMAN_APPROVAL_REQUIRED", str(pv), result)
                    if decision.kind == "approve_step":
                        human_approved = True
                        continue
                    if decision.kind == "skip_step":
                        raise _Skip()
                    if decision.kind == "resume":
                        if step.expect and self._satisfied(step.expect, self.surface.observe()):
                            raise _Skip()
                        continue
                    raise _Stop("ABORTED", "OPERATOR_ABORTED", decision.note, step.step_id)
                raise _Stop("FAILED", pv.code, str(pv), step.step_id)
            except LocatorError as le:
                code = "AMBIGUOUS_TARGET" if isinstance(le, Ambiguous) else "TARGET_NOT_FOUND"
                decision = self._escalate(step, code, str(le), result)
                if decision.kind == "skip_step":
                    raise _Skip()
                if decision.kind in {"resume", "approve_step"}:
                    if step.expect and self._satisfied(step.expect, self.surface.observe()):
                        raise _Skip()
                    continue
                raise _Stop("ABORTED", "OPERATOR_ABORTED", decision.note, step.step_id)

    def _execute(self, step: Step, human_approved: bool) -> Optional[Resolution]:
        self._lease.assert_holder("automation")
        obs = self.surface.observe()
        self._apply_rules(obs, step)            # conditions left by the previous step
        self._builtin_checks(obs, step)
        if step.skip_if and self._satisfied(step.skip_if, obs):
            self._log.event("step.skipped", step_id=step.step_id, reason="skip_if precondition already holds")
            return None

        value = self._subst(step.value) if step.value else None
        control_name = (step.target.name or step.target.label or step.target.anchor_id) if step.target else ""
        url = value if step.action == ActionType.NAVIGATE else None
        if step.requires_human_approval and not human_approved:
            # the artifact itself asks for a person here, regardless of what policy would allow
            raise PolicyViolation("HUMAN_APPROVAL_REQUIRED", f"step {step.step_id} is marked requires_human_approval")
        risk = self.policy.check(step.action, url=url, control_name=control_name,
                                 declared_risk=step.risk, human_approved=human_approved)
        self._log.event("step.start", step_id=step.step_id, action=step.action.value, risk=risk.value,
                        description=step.description, human_approved=human_approved)

        res: Optional[Resolution] = None
        if step.target is not None:
            res = self._resolve_with_wait(step, obs)
            self._log.event("step.resolved", step_id=step.step_id, anchor=step.target.anchor_id, rung=res.rung,
                            confidence=res.confidence, element=res.element.describe() if res.element else None)

        # ---- act
        if step.action == ActionType.NAVIGATE:
            self._navigate(value, step.step_id)
        elif step.action == ActionType.CLICK:
            self._click(res)
        elif step.action == ActionType.TYPE:
            secret = bool(res.element and res.element.input_type == "password") or bool(SECRET_RE.search(step.value or ""))
            self._log.event("step.type", step_id=step.step_id, value="<REDACTED>" if secret else value)
            self.surface.type(res.element.ref, value)
        elif step.action == ActionType.SELECT:
            self.surface.select(res.element.ref, value)
        elif step.action == ActionType.PRESS:
            self.surface.press(value)
        elif step.action == ActionType.READ:
            raw = self.surface.read_text(res.element.ref)
            spec = next(o for o in self._cap.outputs if o.name == step.output)
            val = raw
            if spec.post_process:
                m = re.search(spec.post_process, raw)
                if not m:
                    raise _Stop("FAILED", "OUTPUT_PARSE_FAILED", f"output {spec.name!r}: {spec.post_process!r} did not match",
                                step.step_id, expected=spec.post_process, observed=raw)
                val = m.group(1)
            self._outputs[step.output] = val
            self._log.event("step.read", step_id=step.step_id, output=step.output,
                            value="<REDACTED>" if spec.sensitive else val)
        elif step.action in {ActionType.ASSERT, ActionType.WAIT}:
            pass  # verification below is the whole step

        # ---- verify
        if step.expect:
            self._verify(step.expect, step.step_id, None, step=step)
        self._log.event("step.ok", step_id=step.step_id)
        return res

    # ------------------------------------------------------------ helpers
    @property
    def _outputs(self) -> dict[str, str]:
        return self._outputs_store

    def _all_steps(self, cap: Capability):
        return cap.steps + [s for b in cap.recovery_blocks.values() for s in b]

    def _subst(self, s: str) -> str:
        def p(m):
            k = m.group(1)
            if k not in self._values:
                raise _Stop("FAILED", "MISSING_PARAMETER", f"no value for ${{{k}}}")
            return self._values[k]
        s = PARAM_RE.sub(p, s)
        return SECRET_RE.sub(lambda m: self._secret_values.get(m.group(1), ""), s)

    def _navigate(self, url: str, step_id: str):
        self.policy.check(ActionType.NAVIGATE, url=url)
        self._log.event("navigate", step_id=step_id, url=url)
        self.surface.navigate(url)

    def _click(self, res: Resolution):
        if res.element is not None:
            self.surface.click(res.element.ref)
        else:
            frame, x, y = res.geometric_point
            self._log.event("step.geometric_click", frame=frame, x=x, y=y)
            self.surface.click_xy(frame, x, y)

    def _resolve_with_wait(self, step: Step, obs: Observation) -> Resolution:
        """Targets may appear late (slow legacy pages). Poll — bounded by the policy step timeout."""
        deadline = time.time() + self.policy.step_timeout_ms / 1000
        last_err: Optional[LocatorError] = None
        while True:
            try:
                return resolve(step.target, obs)
            except Ambiguous:
                raise
            except NotFound as e:
                last_err = e
                # the page may be telling us something (error banner, dialog, timeout) — rules first
                self._apply_rules(obs, step)
                self._builtin_checks(obs, step)
                if time.time() > deadline:
                    raise last_err
                time.sleep(POLL_MS / 1000)
                obs = self.surface.observe()

    def _satisfied(self, exp: Expectation, obs: Observation) -> bool:
        text = obs.visible_text
        if exp.text_visible and exp.text_visible.lower() not in text.lower():
            return False
        if exp.text_pattern and not re.search(exp.text_pattern, text, re.I | re.S):
            return False
        if exp.url_pattern and not any(re.search(exp.url_pattern, u) for u in obs.all_locations()):
            return False
        if exp.role_visible:
            try:
                resolve(exp.role_visible, obs)
            except LocatorError:
                return False
        return True

    def _describe(self, exp: Expectation) -> str:
        parts = []
        if exp.text_visible:
            parts.append(f"text visible: {exp.text_visible!r}")
        if exp.text_pattern:
            parts.append(f"text matches: /{exp.text_pattern}/")
        if exp.url_pattern:
            parts.append(f"url matches: /{exp.url_pattern}/")
        if exp.role_visible:
            parts.append(f"control present: {exp.role_visible.role} {exp.role_visible.name or exp.role_visible.label!r}")
        return "; ".join(parts)

    def _verify(self, exp: Expectation, label: str, result: Optional[ReplayResult], step: Optional[Step] = None) -> Observation:
        deadline = time.time() + exp.timeout_ms / 1000
        while True:
            obs = self.surface.observe()
            if self._satisfied(exp, obs):
                return obs
            # not satisfied yet: is the UI showing a known condition instead?
            self._apply_rules(obs, step)
            self._builtin_checks(obs, step)
            if time.time() > deadline:
                snap = self._log.snapshot(self.surface, f"failure-{label}", obs)
                observed = obs.visible_text[:600]
                self._log.event("expectation.failed", step_id=label, expected=self._describe(exp), observed=observed, **snap)
                raise _Stop("FAILED", "EXPECTATION_FAILED", f"expected {self._describe(exp)} after {label}",
                            step_id=label, expected=self._describe(exp), observed=observed)
            time.sleep(POLL_MS / 1000)

    # ------------------------------------------------------------ conditions
    def _rule_matches(self, rule: ConditionRule, obs: Observation) -> Optional[str]:
        m = rule.match
        detail = None
        if m.http_status_min is not None and (obs.http_status is None or obs.http_status < m.http_status_min):
            return None
        if m.text_pattern:
            mm = re.search(m.text_pattern, obs.visible_text, re.I | re.S)
            if not mm:
                return None
            detail = mm.group(0)
        if m.url_pattern and not any(re.search(m.url_pattern, u) for u in obs.all_locations()):
            return None
        if m.anchor_present:
            try:
                resolve(m.anchor_present, obs)
            except LocatorError:
                return None
        return detail or "matched"

    def _apply_rules(self, obs: Observation, step: Optional[Step]):
        if self._in_recovery:
            return  # the triggering condition is still on screen; the block itself is the response
        rule_uses = self._rule_uses
        order = [ConditionKind.BUSINESS_OUTCOME, ConditionKind.RECOVERABLE, ConditionKind.HARD_FAILURE]
        rules = sorted(self._cap.conditions, key=lambda r: order.index(r.kind))
        for rule in rules:
            if rule.applies_after_steps and (step is None or step.step_id not in rule.applies_after_steps):
                continue
            detail = self._rule_matches(rule, obs)
            if detail is None:
                continue
            r = rule.response
            self._log.event("condition.matched", rule_id=rule.rule_id, kind=rule.kind.value, response=r.kind.value,
                            detail=detail, step_id=step.step_id if step else None)
            if r.kind == ResponseKind.RETURN_OUTCOME:
                cap_detail = None
                if r.message_capture:
                    mm = re.search(r.message_capture, obs.visible_text, re.I | re.S)
                    cap_detail = mm.group(1) if mm else None
                self._log.snapshot(self.surface, f"outcome-{r.outcome_code}", obs)
                raise _Stop("BUSINESS_OUTCOME", r.outcome_code, cap_detail or detail, step.step_id if step else None)
            if r.kind == ResponseKind.FAIL:
                raise _Stop("FAILED", r.outcome_code, detail, step.step_id if step else None)
            if r.kind == ResponseKind.ESCALATE:
                decision = self._escalate(step, "UNRECOVERABLE", f"rule {rule.rule_id}: {detail}", None)
                if decision.kind in {"resume", "approve_step", "skip_step"}:
                    raise _Rerun()
                raise _Stop("ABORTED", "OPERATOR_ABORTED", decision.note, step.step_id if step else None)
            # recoverable: bounded
            used = rule_uses.get(rule.rule_id, 0)
            if used >= r.max_attempts:
                raise _Stop("FAILED", "RECOVERY_EXHAUSTED",
                            f"rule {rule.rule_id} fired {used} times without clearing the condition",
                            step.step_id if step else None)
            rule_uses[rule.rule_id] = used + 1
            if r.kind == ResponseKind.DISMISS:
                res = resolve(r.dismiss_target, obs)
                self._log.event("condition.dismiss", rule_id=rule.rule_id, element=res.element.describe() if res.element else None)
                self._click(res)
            elif r.kind == ResponseKind.RETRY:
                self._log.event("condition.retry", rule_id=rule.rule_id, wait_ms=r.wait_ms)
                time.sleep(r.wait_ms / 1000)
            elif r.kind == ResponseKind.RUN_RECOVERY:
                self._log.event("condition.recovery", rule_id=rule.rule_id, block=r.recovery_block)
                self._in_recovery = True
                try:
                    for rs in self._cap.recovery_blocks[r.recovery_block]:
                        self._execute(rs, human_approved=False)
                finally:
                    self._in_recovery = False
                if r.resume_from_step:
                    raise _Restart(r.resume_from_step)
            raise _Rerun()

    def _builtin_checks(self, obs: Observation, step: Optional[Step]):
        """Conditions every capability gets for free, regardless of what the recorder saw."""
        if obs.http_status is not None and obs.http_status >= 500:
            raise _Stop("FAILED", "APP_ERROR", f"application returned HTTP {obs.http_status}",
                        step.step_id if step else None, observed=obs.visible_text[:300])
        dialog = getattr(self.surface, "last_dialog", None)
        if dialog:
            self.surface.last_dialog = None  # type: ignore[attr-defined]
            self._log.event("condition.native_dialog", message=dialog)

    # ------------------------------------------------------------ escalation
    def _escalate(self, step: Optional[Step], code: str, reason: str, result: Optional[ReplayResult]) -> Decision:
        obs = self.surface.observe()
        snap = self._log.snapshot(self.surface, f"intervention-{step.step_id if step else 'run'}", obs)
        req = InterventionRequest(
            request_id=uuid.uuid4().hex[:8], run_id=self._log.run_id, capability_id=self._cap.capability_id,
            goal=self.goal or self._cap.title, step_id=step.step_id if step else None,
            step_description=step.description if step else "", reason_code=code, reason=reason,
            observation_text=obs.render(), screenshot=snap["screenshot"], location=obs.location)
        self._log.event("intervention.requested", **req.summary())
        if result is not None:
            result.interventions += 1
        if self.operator is None:
            raise _Stop("NEEDS_HUMAN", code, reason, step.step_id if step else None, observed=obs.visible_text[:300])
        self._lease.request_intervention(reason)
        human_id = getattr(self.operator, "human_id", "operator")
        self._lease.take(human_id)
        session = LiveSession(self.surface, self._lease, self._log, human_id)
        try:
            decision = self.operator.handle(req, session)
        except Exception as e:  # noqa: BLE001 — an operator crash must return control safely
            decision = Decision("abort", f"operator error: {e}", human_id)
        self._log.event("intervention.decision", kind=decision.kind, note=decision.note, human=human_id)
        if decision.kind == "abort":
            self._lease.abort(f"human:{human_id}", decision.note)
        else:
            self._lease.release(human_id, decision.note)
        return decision
