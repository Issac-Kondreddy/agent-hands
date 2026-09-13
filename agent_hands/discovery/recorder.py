"""
The Recorder turns a successful discovery run into a `Capability`.

It is deliberately *not* a transcript. The model's messages, its reasoning
and the ephemeral element refs are all discarded. What survives is:

  * one `Step` per action, with a semantic `Anchor` built from the element
    the model acted on (role/name/label/table headers/frame/bbox)
  * an auto-derived `Expectation` after each screen-changing action — the
    distinctive text that *appeared* — so replay verifies instead of hoping
  * `${param}` templates instead of the values that were typed; anything
    equal to a secret is replaced with `${secret:NAME}` and never written
  * condition rules the model declared when it met a runtime condition,
    merged with the app profile's product-level rules
  * sign-on steps marked idempotent (`skip_if` the signed-on marker), and the
    profile's "resume from the first post-sign-on step" placeholder resolved
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..locator import anchor_from_element
from ..policy import Policy
from ..schema import (ActionType, Anchor, Capability, Checkpoint, ConditionKind, ConditionMatch, ConditionResponse,
                      ConditionRule, Expectation, Output, OutcomeSpec, Parameter, RiskClass, Step)
from ..surface.base import Element, Observation


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:40] or "x"


class AppProfile:
    def __init__(self, data: dict):
        self.app_id = data["app_id"]
        self.signed_on_marker = Expectation.model_validate(data["signed_on_marker"]) if data.get("signed_on_marker") else None
        self.conditions = [ConditionRule.model_validate(c) for c in data.get("conditions", [])]
        self.recovery_blocks = {k: [Step.model_validate(s) for s in v] for k, v in data.get("recovery_blocks", {}).items()}

    @classmethod
    def load(cls, path: str | Path) -> "AppProfile":
        return cls(json.loads(Path(path).read_text()))


class Recorder:
    def __init__(self, app_id: str, entry_point: str, parameters: list[Parameter], param_values: dict[str, str],
                 secret_values: dict[str, str], policy: Policy, profile: Optional[AppProfile] = None,
                 tenant: Optional[str] = None, model: Optional[str] = None):
        self.app_id, self.entry_point, self.parameters = app_id, entry_point, parameters
        self._values, self._secrets, self.policy, self.profile = param_values, secret_values, policy, profile
        self.tenant, self.model = tenant, model
        self.steps: list[Step] = []
        self.outputs: list[Output] = []
        self.conditions: list[ConditionRule] = []
        self.outcomes: list[OutcomeSpec] = []
        self._anchor_ids: set[str] = set()
        self._observed_values: set[str] = set()   # raw values read during the run — never allowed inside an expectation
        self._signed_on = profile is None or profile.signed_on_marker is None
        self.notes: list[str] = []

    # ------------------------------------------------------------ anchors
    def _anchor_id(self, e: Element, hint: str = "") -> str:
        # ids must be stable across invocations, so a value cell is named by its label/headers, never its text
        headers = f"{e.table.row_header} {e.table.column_header}" if (e.table and e.table.column_header) else ""
        base = slug(hint or e.label or (headers if e.role == "cell" else "") or e.name or headers or e.role)
        aid = f"{base}_{e.role}"
        n = 2
        while aid in self._anchor_ids:
            aid = f"{base}_{e.role}_{n}"
            n += 1
        self._anchor_ids.add(aid)
        return aid

    def _contains_value(self, text: Optional[str]) -> bool:
        return bool(text) and any(v and v in text for v in self._values.values())

    def _anchor(self, e: Element, for_read: bool = False, hint: str = "") -> Anchor:
        a = anchor_from_element(self._anchor_id(e, hint), e)
        if a.table_cell and (self._contains_value(a.table_cell.row_header) or self._contains_value(a.table_cell.column_header)):
            a.table_cell = None   # headers that contain this run's input would only match this run
        if self._contains_value(a.name) and e.role not in {"textbox", "combobox"}:
            a.name = None
        if for_read and e.role == "cell":
            # A value cell's *text* is the value we are reading — it changes per invocation, so it must
            # never be the locating strategy. Prefer the adjacent label or table headers.
            if e.label:
                a.name, a.label, a.table_cell = None, e.label, None
                a.rationale = f"Value cell to the right of the label {e.label!r}; label text is stable, the value is not"
            elif a.table_cell:
                a.name = None
                a.rationale = "Table cell addressed by row/column header text; indexes drift, headers do not"
            else:
                a.rationale = "WARNING: anchored by its own text (no label/headers found) — will only match this value"
                self.notes.append(f"anchor {a.anchor_id}: value-based anchor, review before approval")
        elif e.role in {"textbox", "combobox"}:
            a.rationale = "Role + associated/adjacent label — the way an operator would describe it"
        elif e.role in {"button", "link"}:
            a.rationale = "Role + visible caption; captions are configuration in this product, so tenants override via overlay"
        return a

    # ------------------------------------------------------------ values
    def _template(self, text: str) -> str:
        """Replace typed values with ${param} / ${secret:NAME}. Longest values first."""
        for name, val in sorted(self._secrets.items(), key=lambda kv: -len(kv[1] or "")):
            if val and val in text:
                text = text.replace(val, f"${{secret:{name}}}")
        for name, val in sorted(self._values.items(), key=lambda kv: -len(kv[1] or "")):
            if val and val in text and not text.startswith("${"):
                text = text.replace(val, f"${{{name}}}")
        return text

    # ------------------------------------------------------------ expectations
    @staticmethod
    def derive_expectation(before: Observation, after: Observation) -> Optional[Expectation]:
        """What *appeared* after the action — the first distinctive new heading/cell text."""
        old = {e.name for e in before.elements}
        new = [e for e in after.elements if e.name and e.name not in old and e.role in {"heading", "cell"}]
        for e in new:
            # Legacy headings often embed record data ("Open Sub-Account - Dana R. Whitfield"); keep the
            # invariant prefix only, otherwise the expectation would only ever hold for this one record.
            n = re.split(r"\s+[-–—|]\s+|:\s", e.name.strip(), maxsplit=1)[0].strip()
            if 4 <= len(n) <= 40 and not re.search(r"\d{3,}", n) and not n.endswith(":"):
                return Expectation(text_visible=n)
        new_ctrl = [e for e in after.elements if e.role in {"textbox", "button", "combobox"} and e.name and e.name not in old]
        if new_ctrl:
            e = new_ctrl[0]
            return Expectation(role_visible=Anchor(anchor_id=f"expect_{slug(e.name)}_{e.role}", role=e.role, name=e.name, frame=list(e.frame)))
        return None

    def _post_step(self, step: Step, after: Observation):
        if self.profile and self.profile.signed_on_marker and not self._signed_on:
            step.skip_if = self.profile.signed_on_marker
            from ..locator import LocatorError, resolve  # local import to avoid cycle at module load
            try:
                m = self.profile.signed_on_marker
                if (m.role_visible and resolve(m.role_visible, after)) or (m.text_visible and m.text_visible.lower() in after.visible_text.lower()):
                    self._signed_on = True
                    step.expect = step.expect or m
            except LocatorError:
                pass

    # ------------------------------------------------------------ record
    def record_action(self, action: ActionType, element: Optional[Element], value: Optional[str], before: Observation,
                      after: Observation, reason: str, risk: RiskClass, output: Optional[str] = None,
                      output_desc: str = "", human_approved: bool = False) -> Step:
        reason = self._template(reason)   # a reason like "read balance of member 12345" must not bake the input in
        sid = f"s{len(self.steps) + 1:02d}_{slug(reason.replace('${', ' ').replace('}', ' '))[:24] or action.value}"
        target = self._anchor(element, for_read=(action == ActionType.READ)) if element else None
        tmpl = self._template(value) if value is not None else None
        step = Step(step_id=sid, action=action, target=target, value=tmpl, output=output, risk=risk,
                    description=reason, requires_human_approval=(risk == RiskClass.IRREVERSIBLE),
                    expect=self.derive_expectation(before, after) if action in {ActionType.CLICK, ActionType.PRESS, ActionType.NAVIGATE} else None)
        if output and element and after.find(element.ref):
            self._observed_values.add(after.find(element.ref).name)
        if output:
            self.outputs.append(Output(name=output, description=output_desc or reason,
                                       post_process=r"\$?([\d,]+\.\d{2})" if re.fullmatch(r"\$?[\d,]+\.\d{2}", (after.find(element.ref).name if element and after.find(element.ref) else "") or "") else None))
        self._post_step(step, after)
        self.steps.append(step)
        return step

    def record_assert(self, text: str, reason: str):
        step = Step(step_id=f"s{len(self.steps) + 1:02d}_assert", action=ActionType.ASSERT,
                    expect=Expectation(text_visible=text), description=reason)
        self.steps.append(step)

    def declare_condition(self, kind: str, text_pattern: str, outcome_code: Optional[str], description: str,
                          dismiss_element: Optional[Element] = None) -> ConditionRule:
        rid = slug(outcome_code or description)[:30]
        after_steps = [self.steps[-1].step_id] if self.steps else None
        if kind == "business_outcome":
            rule = ConditionRule(rule_id=rid, kind=ConditionKind.BUSINESS_OUTCOME,
                                 match=ConditionMatch(text_pattern=text_pattern),
                                 response=ConditionResponse(kind="return_outcome", outcome_code=outcome_code,
                                                            message_capture=f"({text_pattern})"),
                                 applies_after_steps=after_steps, description=description)
            self.outcomes.append(OutcomeSpec(code=outcome_code, description=description))
        elif kind == "recoverable":
            rule = ConditionRule(rule_id=rid, kind=ConditionKind.RECOVERABLE,
                                 match=ConditionMatch(text_pattern=text_pattern),
                                 response=ConditionResponse(kind="dismiss", dismiss_target=self._anchor(dismiss_element)),
                                 description=description)
        else:
            rule = ConditionRule(rule_id=rid, kind=ConditionKind.HARD_FAILURE,
                                 match=ConditionMatch(text_pattern=text_pattern),
                                 response=ConditionResponse(kind="fail", outcome_code=outcome_code or "HARD_FAILURE"),
                                 description=description)
        self.conditions.append(rule)
        return rule

    # ------------------------------------------------------------ finalize
    def build(self, capability_id: str, title: str, description: str, checkpoint_text: str,
              tenant: Optional[str] = None) -> Capability:
        conditions = list(self.conditions)
        recovery = {}
        if self.profile:
            first_after = next((s.step_id for s in self.steps if s.skip_if is None), self.steps[0].step_id if self.steps else None)
            for c in self.profile.conditions:
                c = c.model_copy(deep=True)
                if c.response.resume_from_step == "@after_signon":
                    c.response.resume_from_step = first_after
                conditions.append(c)
            recovery = {k: [s.model_copy(deep=True) for s in v] for k, v in self.profile.recovery_blocks.items()}
        # scrub: any expectation that mentions a value we read or were given is record-specific, not a flow invariant
        leaks = {v for v in (*self._observed_values, *self._values.values()) if v and len(v) >= 3}
        for st in self.steps:
            if st.expect and st.expect.text_visible and any(v in st.expect.text_visible for v in leaks):
                self.notes.append(f"step {st.step_id}: dropped record-specific expectation {st.expect.text_visible!r}")
                st.expect = None
        # the checkpoint doubles as the last step's expectation if it had none
        if self.steps and self.steps[-1].expect is None and self.steps[-1].action != ActionType.READ:
            self.steps[-1].expect = Expectation(text_visible=checkpoint_text)
        return Capability(
            capability_id=capability_id, title=title, description=description, app_id=self.app_id,
            surface="legacy_web", entry_point=self.entry_point, recorded_on_tenant=tenant or self.tenant,
            recorded_with_model=self.model, recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            parameters=self.parameters, outputs=self.outputs, outcomes=self.outcomes,
            preconditions=[f"secrets available: {', '.join(sorted(self._secrets))}"] if self._secrets else [],
            steps=self.steps, checkpoint=Checkpoint(description=f"'{checkpoint_text}' is visible", expect=Expectation(text_visible=checkpoint_text)),
            conditions=conditions, recovery_blocks=recovery, notes=self.notes + ["draft: review anchors and rules before approval"],
        )
