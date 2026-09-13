"""
The discovery agent: an LLM-driven observe → decide → act loop.

This is the *only* place a model is in the loop. It runs once per new
capability; its output is a `Capability` artifact that replays without it.

What the model sees:   a compact accessibility rendering of every frame —
                       roles, names, labels, table headers, ephemeral refs.
                       Never a password value. Never a secret.
What the model can do: click / type / select / press / navigate / read /
                       assert_visible / declare_condition / done / stuck.
What it cannot do:     act outside the allowlist, or execute an irreversible
                       control without a human — `Policy.check` runs before
                       every action, exactly as in replay.

Prompting choices (defended in REPORT.md §1):
  * tool use rather than free-text commands — typed args, no parsing
  * the model refers to controls by ref, but the *recorder* stores anchors —
    the model never chooses selectors, so it cannot choose brittle ones
  * parameters are given as `${name}` placeholders with example values; the
    model types the placeholder and the system substitutes, so the recorded
    step is already parameterised
  * secrets are typed by name (`${secret:NAME}`); the model never sees them
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..evidence import EvidenceLog
from ..handoff import ControlLease, Decision, InterventionRequest, LiveSession, Operator
from ..policy import Policy, PolicyViolation
from ..schema import ActionType, Capability, Parameter, RiskClass
from ..surface.base import Element, Observation, Surface, SurfaceError
from .recorder import AppProfile, Recorder

DEFAULT_MODEL = os.environ.get("AGENT_HANDS_MODEL", "claude-sonnet-4-5")

SYSTEM_PROMPT = """You are the hands of an AI agent operating a legacy bank back-office application through its user interface.
You see an accessibility-style rendering of the screen (every frame), with each control tagged by a ref like [f3e2].
You act by calling tools. After every action you receive the new screen.

Rules:
1. Work toward the GOAL with as few actions as possible. Do not explore; do not click things unrelated to the goal.
2. Refer to controls only by the refs in the MOST RECENT screen. Refs change after every action.
3. Parameters are given as placeholders like ${member_id} with an example value. When you must enter a parameter,
   type the placeholder text itself (e.g. "${member_id}"), NOT the example value — the system substitutes it.
4. Credentials are available only as secrets, e.g. "${secret:MERIDIAN_DEMO_PASS}". Type the placeholder; you never see the value.
5. When the goal asks you to read a value, use the `read` tool on the exact cell/field, giving it a snake_case output name.
6. If the screen shows a runtime condition — a validation error, "not found", access denied, a notice/dialog, a timeout —
   call `declare_condition` so the replay engine knows what it means, then continue or finish appropriately.
7. Some controls are irreversible (Confirm, Post, Finalize...). The system may refuse them until a human approves.
   If refused and no human is available, call `stuck` — never try to work around a refusal.
8. When the goal is achieved, call `done` with the text on screen that proves it (the checkpoint) and a capability name.
9. If you cannot make progress after a reasonable attempt, call `stuck` with a clear reason. Never guess at risky actions.
"""

TOOLS = [
    {"name": "click", "description": "Click a control by ref.", "input_schema": {"type": "object", "properties": {
        "ref": {"type": "string"}, "reason": {"type": "string", "description": "Short human-readable purpose, e.g. 'open member lookup'"}},
        "required": ["ref", "reason"]}},
    {"name": "type", "description": "Clear a text field and type into it. Use ${param} / ${secret:NAME} placeholders.",
     "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}, "text": {"type": "string"}, "reason": {"type": "string"}},
                      "required": ["ref", "text", "reason"]}},
    {"name": "select", "description": "Choose an option in a dropdown by its visible text.",
     "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}, "option": {"type": "string"}, "reason": {"type": "string"}},
                      "required": ["ref", "option", "reason"]}},
    {"name": "press", "description": "Press a keyboard key (Enter, Tab, Escape).",
     "input_schema": {"type": "object", "properties": {"key": {"type": "string"}, "reason": {"type": "string"}}, "required": ["key", "reason"]}},
    {"name": "navigate", "description": "Go to a URL inside the allowed application.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}, "reason": {"type": "string"}}, "required": ["url", "reason"]}},
    {"name": "read", "description": "Extract the text of a cell/field as a named output of this capability.",
     "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}, "output_name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                                                       "description": {"type": "string"}}, "required": ["ref", "output_name", "description"]}},
    {"name": "assert_visible", "description": "Record that this text must be visible at this point (a checkpoint inside the flow).",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}, "reason": {"type": "string"}}, "required": ["text", "reason"]}},
    {"name": "declare_condition", "description": "Teach the replay engine what a runtime condition on screen means.",
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["business_outcome", "recoverable", "hard_failure"],
                  "description": "business_outcome = legitimate answer for the caller (not found, denied, validation); recoverable = dismiss and continue; hard_failure = stop"},
         "text_pattern": {"type": "string", "description": "Regex matching the on-screen text of the condition, generalised (no specific ids)"},
         "outcome_code": {"type": "string", "description": "UPPER_SNAKE code for business_outcome/hard_failure, e.g. MEMBER_NOT_FOUND"},
         "dismiss_ref": {"type": "string", "description": "For recoverable: ref of the control that dismisses it"},
         "description": {"type": "string"}}, "required": ["kind", "text_pattern", "description"]}},
    {"name": "done", "description": "The goal is achieved. Finalise the capability.",
     "input_schema": {"type": "object", "properties": {
         "checkpoint_text": {"type": "string", "description": "Text visible on screen right now that proves success"},
         "capability_id": {"type": "string", "pattern": "^[a-z][a-z0-9_.]*$", "description": "e.g. meridian_core.member_savings_balance"},
         "title": {"type": "string"}, "description": {"type": "string", "description": "What this capability does, for an agent deciding whether to call it"}},
         "required": ["checkpoint_text", "capability_id", "title", "description"]}},
    {"name": "stuck", "description": "You cannot safely proceed. A human will be asked to take over the live session.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]


@dataclass
class DiscoveryResult:
    status: str                       # SUCCESS | STUCK | FAILED | BUSINESS_OUTCOME
    run_id: str
    evidence_dir: str
    capability: Optional[Capability] = None
    artifact_path: Optional[str] = None
    detail: str = ""
    model_calls: int = 0
    actions: int = 0
    interventions: int = 0
    duration_ms: int = 0


class DiscoveryAgent:
    def __init__(self, surface: Surface, policy: Policy, *, app_id: str, entry_point: str, parameters: list[Parameter],
                 param_values: dict[str, str], secret_names: list[str], profile: Optional[AppProfile] = None,
                 tenant: Optional[str] = None, model: str = DEFAULT_MODEL, evidence_dir: str = "evidence/runs",
                 artifacts_dir: str = "artifacts", operator: Optional[Operator] = None, max_steps: Optional[int] = None,
                 client=None):
        self.surface, self.policy, self.app_id, self.entry_point = surface, policy, app_id, entry_point
        self.parameters, self.values, self.secret_names = parameters, param_values, secret_names
        self.profile, self.tenant, self.model = profile, tenant, model
        self.evidence_dir, self.artifacts_dir, self.operator = evidence_dir, artifacts_dir, operator
        self.max_steps = max_steps or policy.max_steps
        self._client = client
        self._secrets = {n: os.environ.get(n, "") for n in secret_names}

    # ------------------------------------------------------------ llm
    def _llm(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def _resolve_model(self) -> str:
        """If the configured model id is unknown to the API, fall back to the newest available Sonnet."""
        try:
            self._llm().models.retrieve(self.model)
            return self.model
        except Exception:  # noqa: BLE001
            try:
                ids = [m.id for m in self._llm().models.list(limit=100).data]
                for pref in ("sonnet", "opus", "haiku"):
                    for mid in ids:
                        if pref in mid:
                            return mid
            except Exception:  # noqa: BLE001
                pass
            return self.model

    # ------------------------------------------------------------ run
    def run(self, goal: str, enrich: Optional[Capability] = None) -> DiscoveryResult:
        """
        Discover a new capability, or — with `enrich` — run the same goal against inputs that
        produce a runtime condition, and fold whatever the model declares into a new version
        of the existing artifact. The flow steps of `enrich` are kept verbatim; only
        conditions/outcomes are added. Every version is a reviewable diff.
        """
        t0 = time.time()
        self._enrich = enrich
        redact = self.policy.redactor({f"secret:{k}": v for k, v in self._secrets.items() if v})
        log = EvidenceLog.start(self.evidence_dir, "discovery", redact,
                                meta={"goal": goal, "app_id": self.app_id, "entry_point": self.entry_point,
                                      "parameters": {p.name: self.values.get(p.name) for p in self.parameters}})
        self._log, self._lease = log, ControlLease(log)
        rec = Recorder(self.app_id, self.entry_point, self.parameters, self.values, self._secrets, self.policy,
                       self.profile, self.tenant, self.model)
        self._rec = rec
        result = DiscoveryResult(status="FAILED", run_id=log.run_id, evidence_dir=str(log.root))
        model = self._resolve_model()
        result_holder = {"final": None}
        try:
            self.policy.check(ActionType.NAVIGATE, url=self.entry_point)
            self.surface.navigate(self.entry_point)
            obs = self.surface.observe()
            log.snapshot(self.surface, "step-00", obs)
            param_desc = "\n".join(f"  ${{{p.name}}} ({p.type.value}): {p.description or ''} — example value: {self.values.get(p.name)!r}"
                                   for p in self.parameters) or "  (none)"
            secret_desc = ", ".join(f"${{secret:{n}}}" for n in self.secret_names) or "(none)"
            messages = [{"role": "user", "content": f"GOAL: {goal}\n\nPARAMETERS:\n{param_desc}\n\nSECRETS (type the placeholder): {secret_desc}\n\n"
                                                    f"CURRENT SCREEN:\n{obs.render()}"}]
            for turn in range(self.max_steps):
                resp = self._llm().messages.create(model=model, max_tokens=1500, system=SYSTEM_PROMPT, tools=TOOLS, messages=messages)
                result.model_calls += 1
                log.event("llm.response", stop_reason=resp.stop_reason,
                          text=" ".join(b.text for b in resp.content if b.type == "text")[:2000],
                          tools=[{"name": b.name, "input": b.input} for b in resp.content if b.type == "tool_use"],
                          usage={"in": resp.usage.input_tokens, "out": resp.usage.output_tokens})
                messages.append({"role": "assistant", "content": resp.content})
                tool_uses = [b for b in resp.content if b.type == "tool_use"]
                if not tool_uses:
                    messages.append({"role": "user", "content": "Use a tool. If the goal is met call done; if not, act or call stuck."})
                    continue
                tool_results = []
                for tu in tool_uses:
                    out, final = self._dispatch(tu.name, tu.input, obs, result)
                    if final is not None:
                        result_holder["final"] = final
                    if final is None:
                        obs = self.surface.observe()
                        result.actions += 1
                        log.snapshot(self.surface, f"step-{result.actions:02d}", obs)
                        out = f"{out}\n\nCURRENT SCREEN:\n{obs.render()}"
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": redact(out)})
                    if final is not None:
                        break
                messages.append({"role": "user", "content": tool_results})
                if result_holder["final"] is not None:
                    break
            final = result_holder["final"]
            if final is None:
                result.status, result.detail = "FAILED", f"max_steps ({self.max_steps}) reached without done/stuck"
            elif final["kind"] == "done":
                cap = rec.build(final["capability_id"], final["title"], final["description"], final["checkpoint_text"])
                # the checkpoint must hold *right now* — otherwise the model is claiming success it cannot show
                if final["checkpoint_text"].lower() not in obs.visible_text.lower():
                    result.status, result.detail = "FAILED", f"model claimed done but checkpoint text {final['checkpoint_text']!r} is not on screen"
                else:
                    Path(self.artifacts_dir).mkdir(parents=True, exist_ok=True)
                    path = Path(self.artifacts_dir) / f"{cap.capability_id}.v{cap.version}.json"
                    path.write_text(cap.to_json())
                    result.status, result.capability, result.artifact_path = "SUCCESS", cap, str(path)
                    log.event("artifact.saved", path=str(path), fingerprint=cap.fingerprint(), steps=len(cap.steps))
            elif final["kind"] == "stuck":
                result.status, result.detail = "STUCK", final["reason"]
            elif final["kind"] == "outcome":
                result.status, result.detail = "BUSINESS_OUTCOME", final["reason"]
            if enrich is not None and rec.conditions:
                new = enrich.model_copy(deep=True)
                known = {r.rule_id for r in new.conditions}
                added = [r.model_copy(update={"applies_after_steps": None}) for r in rec.conditions if r.rule_id not in known]
                if added:
                    new.conditions = added + new.conditions
                    codes = {o.code for o in new.outcomes}
                    new.outcomes = new.outcomes + [o for o in rec.outcomes if o.code not in codes]
                    new.version, new.status = enrich.version + 1, "draft"
                    new.notes = new.notes + [f"v{new.version}: learned {[r.rule_id for r in added]} from run {log.run_id}"]
                    new = Capability.model_validate(new.model_dump())
                    Path(self.artifacts_dir).mkdir(parents=True, exist_ok=True)
                    path = Path(self.artifacts_dir) / f"{new.capability_id}.v{new.version}.json"
                    path.write_text(new.to_json())
                    result.capability, result.artifact_path = new, str(path)
                    result.status = "ENRICHED"
                    log.event("artifact.enriched", path=str(path), added=[r.rule_id for r in added], version=new.version)
        except SurfaceError as e:
            result.status, result.detail = "FAILED", f"surface error: {e}"
            log.snapshot(self.surface, "failure")
        result.duration_ms = int((time.time() - t0) * 1000)
        log.finish({"status": result.status, "detail": result.detail, "artifact": result.artifact_path,
                    "model": model, "model_calls": result.model_calls, "actions": result.actions,
                    "interventions": result.interventions})
        return result

    # ------------------------------------------------------------ tools
    def _subst(self, text: str) -> str:
        for k, v in self.values.items():
            text = text.replace(f"${{{k}}}", v)
        for k, v in self._secrets.items():
            text = text.replace(f"${{secret:{k}}}", v)
        return text

    def _el(self, obs: Observation, ref: str) -> Element:
        e = obs.find(ref)
        if e is None:
            raise SurfaceError(f"ref {ref!r} is not on the current screen — use refs from the latest screen only")
        return e

    def _dispatch(self, name: str, args: dict, obs: Observation, result: DiscoveryResult):
        """Returns (text_for_model, final_dict_or_None)."""
        rec, log = self._rec, self._log
        try:
            self._lease.assert_holder("automation")
            if name == "click":
                e = self._el(obs, args["ref"])
                declared = RiskClass.READ_ONLY if e.role == "link" else RiskClass.REVERSIBLE
                risk = self._gate(ActionType.CLICK, e, declared, args["reason"], result)
                if risk is None:
                    return ("REFUSED: this control is classified irreversible and requires human approval; none was granted. "
                            "Call stuck if the goal cannot be reached without it."), None
                log.event("agent.action", action="click", target=e.describe(), reason=args["reason"], risk=risk.value)
                self.surface.click(e.ref)
                after = self.surface.observe()
                rec.record_action(ActionType.CLICK, e, None, obs, after, args["reason"], risk)
                return f"clicked {e.describe()}", None
            if name == "type":
                e = self._el(obs, args["ref"])
                text = args["text"]
                secret = e.input_type == "password" or "${secret:" in text
                self.policy.check(ActionType.TYPE, control_name=e.name, declared_risk=RiskClass.REVERSIBLE)
                log.event("agent.action", action="type", target=e.describe(), value="<REDACTED>" if secret else text, reason=args["reason"])
                self.surface.type(e.ref, self._subst(text))
                after = self.surface.observe()
                rec.record_action(ActionType.TYPE, e, text, obs, after, args["reason"], RiskClass.REVERSIBLE)
                return f"typed into {e.describe()}", None
            if name == "select":
                e = self._el(obs, args["ref"])
                self.policy.check(ActionType.SELECT, control_name=e.name, declared_risk=RiskClass.REVERSIBLE)
                log.event("agent.action", action="select", target=e.describe(), value=args["option"], reason=args["reason"])
                self.surface.select(e.ref, self._subst(args["option"]))
                after = self.surface.observe()
                rec.record_action(ActionType.SELECT, e, args["option"], obs, after, args["reason"], RiskClass.REVERSIBLE)
                return f"selected {args['option']!r}", None
            if name == "press":
                self.policy.check(ActionType.PRESS)
                log.event("agent.action", action="press", key=args["key"], reason=args["reason"])
                self.surface.press(args["key"])
                after = self.surface.observe()
                rec.record_action(ActionType.PRESS, None, args["key"], obs, after, args["reason"], RiskClass.REVERSIBLE)
                return f"pressed {args['key']}", None
            if name == "navigate":
                self.policy.check(ActionType.NAVIGATE, url=args["url"])
                log.event("agent.action", action="navigate", url=args["url"], reason=args["reason"])
                self.surface.navigate(args["url"])
                after = self.surface.observe()
                rec.record_action(ActionType.NAVIGATE, None, args["url"], obs, after, args["reason"], RiskClass.READ_ONLY)
                return f"navigated to {args['url']}", None
            if name == "read":
                e = self._el(obs, args["ref"])
                val = self.surface.read_text(e.ref)
                log.event("agent.action", action="read", target=e.describe(), output=args["output_name"], value=val)
                rec.record_action(ActionType.READ, e, None, obs, obs, args["description"], RiskClass.READ_ONLY,
                                  output=args["output_name"], output_desc=args["description"])
                return f"read {args['output_name']} = {val!r}", None
            if name == "assert_visible":
                if args["text"].lower() not in obs.visible_text.lower():
                    return f"ASSERTION FAILED: {args['text']!r} is not visible", None
                rec.record_assert(args["text"], args["reason"])
                return "assertion recorded", None
            if name == "declare_condition":
                dismiss = self._el(obs, args["dismiss_ref"]) if args.get("dismiss_ref") else None
                rule = rec.declare_condition(args["kind"], args["text_pattern"], args.get("outcome_code"), args["description"], dismiss)
                log.event("agent.condition", rule_id=rule.rule_id, kind=rule.kind.value, pattern=args["text_pattern"])
                if rule.kind.value == "business_outcome":
                    return (f"condition {rule.rule_id} recorded as business outcome {args.get('outcome_code')}. "
                            "If this outcome means the goal cannot be completed with these inputs, say so and call stuck with reason "
                            "'BUSINESS_OUTCOME: <code>'; otherwise continue."), None
                return f"condition {rule.rule_id} recorded", None
            if name == "done":
                return "finalising", {"kind": "done", **args}
            if name == "stuck":
                reason = args["reason"]
                if reason.upper().startswith("BUSINESS_OUTCOME"):
                    return "recorded", {"kind": "outcome", "reason": reason}
                decision = self._escalate("STUCK", reason, result)
                if decision is None or decision.kind == "abort":
                    return "stopping", {"kind": "stuck", "reason": reason}
                return f"A human operator intervened on the live session ({decision.note!r}) and handed control back. Continue.", None
            return f"unknown tool {name}", None
        except PolicyViolation as pv:
            log.event("policy.violation", code=pv.code, message=str(pv))
            return f"REFUSED by policy: {pv}", None
        except SurfaceError as e:
            log.event("surface.error", message=str(e))
            return f"ERROR: {e}", None

    def _gate(self, action: ActionType, e: Element, declared: RiskClass, reason: str, result: DiscoveryResult) -> Optional[RiskClass]:
        try:
            return self.policy.check(action, control_name=e.name, declared_risk=declared)
        except PolicyViolation as pv:
            if pv.code != "HUMAN_APPROVAL_REQUIRED":
                raise
            self._log.event("policy.gate", code=pv.code, control=e.name, reason=reason)
            decision = self._escalate("HUMAN_APPROVAL_REQUIRED", f"{e.describe()} — {reason}", result)
            if decision is not None and decision.kind == "approve_step":
                return self.policy.check(action, control_name=e.name, declared_risk=declared, human_approved=True)
            return None

    def _escalate(self, code: str, reason: str, result: DiscoveryResult) -> Optional[Decision]:
        obs = self.surface.observe()
        snap = self._log.snapshot(self.surface, f"intervention-{result.actions:02d}", obs)
        req = InterventionRequest(request_id=uuid.uuid4().hex[:8], run_id=self._log.run_id, capability_id="(discovery)",
                                  goal="discovery", step_id=None, step_description=reason, reason_code=code, reason=reason,
                                  observation_text=obs.render(), screenshot=snap["screenshot"], location=obs.location)
        self._log.event("intervention.requested", **req.summary())
        result.interventions += 1
        if self.operator is None:
            return None
        self._lease.request_intervention(reason)
        human_id = getattr(self.operator, "human_id", "operator")
        self._lease.take(human_id)

        def on_action(action: str, el: Optional[Element], value: Optional[str], before: Observation):
            # a human's manual steps become recorded steps, flagged so reviewers can see their provenance
            after = self.surface.observe()
            risk = RiskClass.IRREVERSIBLE if (el and self.policy.classify_control(el.name, RiskClass.REVERSIBLE) == RiskClass.IRREVERSIBLE) else RiskClass.REVERSIBLE
            self._rec.record_action(ActionType(action), el, value, before or after, after, f"(human) {action} {el.name if el else ''}".strip(), risk)

        session = LiveSession(self.surface, self._lease, self._log, human_id, on_action=on_action)
        try:
            decision = self.operator.handle(req, session)
        except Exception as ex:  # noqa: BLE001
            decision = Decision("abort", f"operator error: {ex}", human_id)
        self._log.event("intervention.decision", kind=decision.kind, note=decision.note, human=human_id)
        if decision.kind == "abort":
            self._lease.abort(f"human:{human_id}", decision.note)
        else:
            self._lease.release(human_id, decision.note)
        return decision
