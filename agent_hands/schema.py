"""
The capability artifact schema — the contract between "the model discovered
how to do this once" and "an AI agent invokes it a thousand times".

Design principles (defended in REPORT.md §2):

1. **Semantic anchors, not selectors.** A target is described the way a
   human operator would describe it ("the *Search* button", "the text box
   labelled *Member Number*", "the *Current Balance* cell in the row whose
   *Account Type* is *Regular Savings*"). Each anchor carries an ordered
   ladder of strategies; replay walks down the ladder and records which rung
   resolved. This is what lets the same artifact survive a legacy web app or
   a desktop app — both expose roles/names/labels/geometry, neither promises
   ids or CSS.

2. **A capability is a function.** It has a name, a version, typed
   parameters, typed outputs, pre-/post-conditions. The artifact is the
   contract an agent reads to decide whether and how to call it.

3. **Runtime conditions are first-class.** `ConditionRule`s say what the
   replay engine should do when the UI shows a validation error, a
   not-found result, a permission denial, an interstitial, or a session
   expiry — and, crucially, whether that is a *business outcome*
   (return it to the caller), a *recoverable* condition (handle and
   continue) or a *hard failure* (stop, surface, escalate).

4. **Never store values, store references.** Parameter *values* are never
   serialized into the artifact; steps reference `${param}` placeholders.
   Sensitive parameters are declared so the evidence layer redacts them.

5. **Tenant portability is an overlay, not a fork.** Anchors are keyed by a
   stable `anchor_id`; a `TenantOverlay` remaps names/labels for a tenant
   running the same vendor product without touching the base flow.
"""
from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"
PARAM_RE = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    PRESS = "press"
    READ = "read"           # extract text into an output
    ASSERT = "assert"       # checkpoint: something must be visible / match
    WAIT = "wait"           # wait for a condition (never a bare sleep)


class RiskClass(str, Enum):
    """Safety classification of a step. Drives the guardrail policy."""
    READ_ONLY = "read_only"         # observing, navigating within the app
    REVERSIBLE = "reversible"       # typing into a form, opening a menu
    IRREVERSIBLE = "irreversible"   # commits money/records — Confirm, Submit, Post


class ConditionKind(str, Enum):
    BUSINESS_OUTCOME = "business_outcome"   # a legitimate answer for the caller
    RECOVERABLE = "recoverable"             # handle it and carry on
    HARD_FAILURE = "hard_failure"           # stop, surface, maybe escalate


class ResponseKind(str, Enum):
    RETURN_OUTCOME = "return_outcome"   # stop replay, report outcome_code to caller
    DISMISS = "dismiss"                 # click an anchor, then re-run the current step
    RETRY = "retry"                     # wait and re-run the current step (bounded)
    RUN_RECOVERY = "run_recovery"       # execute a named recovery block, then re-run step
    FAIL = "fail"                       # hard failure with this code
    ESCALATE = "escalate"               # hand the live session to a human


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    MONEY = "money"


# ---------------------------------------------------------------------------
# Targeting: anchors and the locator ladder
# ---------------------------------------------------------------------------
class TableCell(BaseModel):
    """Locate a cell by *header text*, not by row/col index — indexes drift, headers don't."""
    row_header: str = Field(..., description="Text in the row's key cell, e.g. 'Regular Savings'")
    row_header_column: Optional[str] = Field(None, description="Column header of the row key, e.g. 'Account Type'")
    column_header: str = Field(..., description="Column header of the wanted cell, e.g. 'Current Balance'")


class Anchor(BaseModel):
    """
    A surface-agnostic description of one UI control.

    Every field is optional except `anchor_id`; the ladder uses whatever is
    present, most-specific first. `frame` is a *name path* (e.g. ["main"]),
    never an index — frames in legacy apps are named, and names are stable.
    """
    anchor_id: str = Field(..., description="Stable id for overlays and evidence, e.g. 'member_number_input'")
    role: Optional[str] = Field(None, description="Accessibility role: button, textbox, link, combobox, cell ...")
    name: Optional[str] = Field(None, description="Accessible name as shown to a human: 'Search', 'Member Number'")
    label: Optional[str] = Field(None, description="Visible label text adjacent to / associated with the control")
    near_text: Optional[str] = Field(None, description="Distinctive text physically near the control (last-resort semantic hint)")
    table_cell: Optional[TableCell] = None
    frame: list[str] = Field(default_factory=list, description="Frame name path from the top document")
    bbox: Optional[list[float]] = Field(None, description="[x, y, w, h] at record time — geometric fallback only")
    allow_geometric_fallback: bool = Field(False, description="Permit clicking the recorded bbox if all semantic rungs fail")
    rationale: Optional[str] = Field(None, description="Why this anchor is expected to be robust (recorded for reviewers)")

    @model_validator(mode="after")
    def _must_have_some_strategy(self):
        if not any([self.role, self.name, self.label, self.near_text, self.table_cell, self.bbox]):
            raise ValueError(f"anchor {self.anchor_id!r} has no locating strategy")
        return self


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
class Expectation(BaseModel):
    """Post-condition for a step. This is what makes replay *verify* rather than *hope*."""
    text_visible: Optional[str] = Field(None, description="Substring that must be visible (case-insensitive)")
    text_pattern: Optional[str] = Field(None, description="Regex that must match visible text")
    role_visible: Optional[Anchor] = Field(None, description="An anchor that must resolve")
    url_pattern: Optional[str] = Field(None, description="Regex the current URL must match (web surfaces only)")
    timeout_ms: int = Field(8000, ge=100, le=120_000)

    @model_validator(mode="after")
    def _non_empty(self):
        if not any([self.text_visible, self.text_pattern, self.role_visible, self.url_pattern]):
            raise ValueError("expectation must assert at least one thing")
        return self


class Step(BaseModel):
    step_id: str
    action: ActionType
    target: Optional[Anchor] = None
    value: Optional[str] = Field(None, description="Literal or '${param}' template; never a raw secret")
    output: Optional[str] = Field(None, description="For READ: the output name this step fills")
    risk: RiskClass = RiskClass.READ_ONLY
    expect: Optional[Expectation] = Field(None, description="Post-condition verified before the next step")
    skip_if: Optional[Expectation] = Field(None, description="If this already holds when the step is reached, the step is unnecessary (idempotency, e.g. 'already signed on')")
    description: str = ""
    requires_human_approval: bool = Field(False, description="Pause and hand off before executing (set by policy for irreversible steps)")

    @model_validator(mode="after")
    def _shape(self):
        needs_target = {ActionType.CLICK, ActionType.TYPE, ActionType.SELECT, ActionType.READ}
        if self.action in needs_target and self.target is None:
            raise ValueError(f"step {self.step_id}: {self.action.value} requires a target")
        if self.action in {ActionType.TYPE, ActionType.SELECT, ActionType.NAVIGATE, ActionType.PRESS} and not self.value:
            raise ValueError(f"step {self.step_id}: {self.action.value} requires a value")
        if self.action == ActionType.READ and not self.output:
            raise ValueError(f"step {self.step_id}: read requires an output name")
        if self.action in {ActionType.ASSERT, ActionType.WAIT} and self.expect is None:
            raise ValueError(f"step {self.step_id}: {self.action.value} requires an expectation")
        return self

    def param_refs(self) -> set[str]:
        return set(PARAM_RE.findall(self.value or ""))


# ---------------------------------------------------------------------------
# Runtime conditions
# ---------------------------------------------------------------------------
class ConditionMatch(BaseModel):
    text_pattern: Optional[str] = Field(None, description="Regex over visible text of the observed surface")
    anchor_present: Optional[Anchor] = None
    url_pattern: Optional[str] = None
    http_status_min: Optional[int] = Field(None, description="Main-document HTTP status >= this (web only)")

    @model_validator(mode="after")
    def _non_empty(self):
        if not any([self.text_pattern, self.anchor_present, self.url_pattern, self.http_status_min]):
            raise ValueError("condition match must test at least one thing")
        return self


class ConditionResponse(BaseModel):
    kind: ResponseKind
    outcome_code: Optional[str] = Field(None, description="For RETURN_OUTCOME / FAIL: machine-readable code, e.g. MEMBER_NOT_FOUND")
    dismiss_target: Optional[Anchor] = None
    recovery_block: Optional[str] = None
    resume_from_step: Optional[str] = Field(None, description="After RUN_RECOVERY, restart the flow from this step (state was reset)")
    max_attempts: int = Field(2, ge=1, le=5, description="Per run — bounds every recoverable rule so replay always terminates")
    wait_ms: int = Field(1000, ge=0, le=60_000)
    message_capture: Optional[str] = Field(None, description="Regex with one group; captured text is returned as outcome detail")


class ConditionRule(BaseModel):
    """
    'When the surface looks like X, it means Y, so do Z.'

    Evaluated after every step (and whenever a step fails to find its
    target). Order matters: first match wins. Business outcomes are checked
    before recoverables before hard failures so that "No record found" is
    never mistaken for a broken locator.
    """
    rule_id: str
    kind: ConditionKind
    match: ConditionMatch
    response: ConditionResponse
    applies_after_steps: Optional[list[str]] = Field(None, description="Restrict to these step_ids; None = any step")
    description: str = ""

    @model_validator(mode="after")
    def _consistent(self):
        r, k = self.response, self.kind
        if k == ConditionKind.BUSINESS_OUTCOME and r.kind != ResponseKind.RETURN_OUTCOME:
            raise ValueError(f"rule {self.rule_id}: business outcomes must RETURN_OUTCOME")
        if k == ConditionKind.RECOVERABLE and r.kind not in {ResponseKind.DISMISS, ResponseKind.RETRY, ResponseKind.RUN_RECOVERY}:
            raise ValueError(f"rule {self.rule_id}: recoverable rules must DISMISS, RETRY or RUN_RECOVERY")
        if k == ConditionKind.HARD_FAILURE and r.kind not in {ResponseKind.FAIL, ResponseKind.ESCALATE}:
            raise ValueError(f"rule {self.rule_id}: hard failures must FAIL or ESCALATE")
        if r.kind in {ResponseKind.RETURN_OUTCOME, ResponseKind.FAIL} and not r.outcome_code:
            raise ValueError(f"rule {self.rule_id}: outcome_code required")
        if r.kind == ResponseKind.DISMISS and r.dismiss_target is None:
            raise ValueError(f"rule {self.rule_id}: dismiss_target required")
        if r.kind == ResponseKind.RUN_RECOVERY and not r.recovery_block:
            raise ValueError(f"rule {self.rule_id}: recovery_block required")
        return self


# ---------------------------------------------------------------------------
# Contract: parameters, outputs, checkpoint
# ---------------------------------------------------------------------------
class Parameter(BaseModel):
    name: str = Field(..., pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    type: ParamType = ParamType.STRING
    description: str = ""
    required: bool = True
    pattern: Optional[str] = Field(None, description="Regex the value must match before replay starts")
    sensitive: bool = Field(False, description="Redact from all evidence/logs; never echo back")
    example: Optional[str] = None


class Output(BaseModel):
    name: str = Field(..., pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    type: ParamType = ParamType.STRING
    description: str = ""
    sensitive: bool = False
    post_process: Optional[str] = Field(None, description="Regex with one capture group applied to raw text, e.g. strip '$'")


class Checkpoint(BaseModel):
    """The success condition. Reaching the last step is not success; *this* is."""
    description: str
    expect: Expectation


class OutcomeSpec(BaseModel):
    """Declared business outcomes, so a calling agent knows every non-success answer it may receive."""
    code: str
    description: str


class Capability(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    capability_id: str = Field(..., pattern=r"^[a-z][a-z0-9_.]*$", description="e.g. 'meridian_core.member_savings_balance'")
    version: int = Field(1, ge=1)
    status: Literal["draft", "approved", "deprecated"] = "draft"
    title: str
    description: str
    app_id: str = Field(..., description="The vendor product this flow drives, e.g. 'meridian_core'")
    surface: Literal["web", "legacy_web", "desktop"] = "legacy_web"
    entry_point: str = Field(..., description="Where replay starts — URL template for web, app/window for desktop")
    recorded_on_tenant: Optional[str] = None
    recorded_with_model: Optional[str] = None
    recorded_at: Optional[str] = None
    parameters: list[Parameter] = Field(default_factory=list)
    outputs: list[Output] = Field(default_factory=list)
    outcomes: list[OutcomeSpec] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list, description="Human-readable, e.g. 'operator session signed on'")
    steps: list[Step]
    checkpoint: Checkpoint
    conditions: list[ConditionRule] = Field(default_factory=list)
    recovery_blocks: dict[str, list[Step]] = Field(default_factory=dict)
    max_risk: RiskClass = Field(RiskClass.READ_ONLY, description="Highest risk class any step carries (derived)")
    notes: list[str] = Field(default_factory=list)

    # --- validation --------------------------------------------------------
    @model_validator(mode="after")
    def _cross_checks(self):
        ids = [s.step_id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step_id")
        if not self.steps:
            raise ValueError("capability needs at least one step")
        declared_params = {p.name for p in self.parameters}
        declared_outputs = {o.name for o in self.outputs}
        for s in self.steps + [x for b in self.recovery_blocks.values() for x in b]:
            missing = s.param_refs() - declared_params
            if missing:
                raise ValueError(f"step {s.step_id} references undeclared parameter(s) {sorted(missing)}")
            if s.action == ActionType.READ and s.output not in declared_outputs:
                raise ValueError(f"step {s.step_id} fills undeclared output {s.output!r}")
        for r in self.conditions:
            if r.response.recovery_block and r.response.recovery_block not in self.recovery_blocks:
                raise ValueError(f"rule {r.rule_id} references unknown recovery block {r.response.recovery_block!r}")
            if r.applies_after_steps:
                unknown = set(r.applies_after_steps) - set(ids)
                if unknown:
                    raise ValueError(f"rule {r.rule_id} references unknown steps {sorted(unknown)}")
            if r.response.resume_from_step and r.response.resume_from_step not in ids:
                raise ValueError(f"rule {r.rule_id} resume_from_step {r.response.resume_from_step!r} is not a step")
        # derive max_risk
        order = [RiskClass.READ_ONLY, RiskClass.REVERSIBLE, RiskClass.IRREVERSIBLE]
        self.max_risk = max((s.risk for s in self.steps), key=order.index, default=RiskClass.READ_ONLY)
        # every step must be verifiable somewhere: at least the checkpoint exists (enforced by type)
        return self

    @field_validator("entry_point")
    @classmethod
    def _no_secrets_in_entry(cls, v: str):
        if re.search(r"(password|pwd|token|secret|api[_-]?key)=", v, re.I):
            raise ValueError("entry_point must not embed credentials")
        return v

    # --- helpers -----------------------------------------------------------
    def fingerprint(self) -> str:
        """Content hash of the flow (steps+conditions+contract). Changes → new version."""
        payload = self.model_dump(mode="json", exclude={"recorded_at", "notes", "status"})
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def validate_inputs(self, params: dict[str, Any]) -> dict[str, str]:
        """Type-check and coerce caller-supplied inputs. Raises ValueError with a caller-friendly message."""
        out: dict[str, str] = {}
        for p in self.parameters:
            if p.name not in params or params[p.name] in (None, ""):
                if p.required:
                    raise ValueError(f"missing required parameter {p.name!r}")
                continue
            raw = str(params[p.name])
            if p.type == ParamType.INTEGER and not re.fullmatch(r"-?\d+", raw):
                raise ValueError(f"parameter {p.name!r} must be an integer")
            if p.type in {ParamType.NUMBER, ParamType.MONEY} and not re.fullmatch(r"-?\d+(\.\d+)?", raw):
                raise ValueError(f"parameter {p.name!r} must be numeric")
            if p.type == ParamType.BOOLEAN and raw.lower() not in {"true", "false"}:
                raise ValueError(f"parameter {p.name!r} must be true/false")
            if p.pattern and not re.fullmatch(p.pattern, raw):
                raise ValueError(f"parameter {p.name!r} does not match required pattern {p.pattern}")
            out[p.name] = raw
        unknown = set(params) - {p.name for p in self.parameters}
        if unknown:
            raise ValueError(f"unknown parameter(s) {sorted(unknown)}")
        return out

    def sensitive_param_names(self) -> set[str]:
        return {p.name for p in self.parameters if p.sensitive}

    def to_json(self) -> str:
        return self.model_dump_json(indent=2, exclude_none=True)

    @classmethod
    def from_json(cls, text: str) -> "Capability":
        return cls.model_validate_json(text)

    def agent_facing_contract(self) -> dict:
        """What an AI agent sees in the capability catalog — no steps, just the function signature."""
        return {
            "name": self.capability_id,
            "version": self.version,
            "status": self.status,
            "description": self.description,
            "parameters": {p.name: {"type": p.type.value, "required": p.required, "description": p.description,
                                    **({"pattern": p.pattern} if p.pattern else {})} for p in self.parameters},
            "returns": {o.name: {"type": o.type.value, "description": o.description} for o in self.outputs},
            "possible_outcomes": {o.code: o.description for o in self.outcomes},
            "max_risk": self.max_risk.value,
            "requires_human_approval": any(s.requires_human_approval for s in self.steps),
        }


# ---------------------------------------------------------------------------
# Tenant overlay
# ---------------------------------------------------------------------------
class AnchorOverride(BaseModel):
    name: Optional[str] = None
    label: Optional[str] = None
    near_text: Optional[str] = None
    table_cell: Optional[TableCell] = None
    frame: Optional[list[str]] = None


class TenantOverlay(BaseModel):
    """
    Per-tenant specialisation of a base capability: same flow, different words.

    Applied at load time; the base artifact is untouched. If a tenant needs a
    *different flow* (extra step, missing screen) that is a fork, not an
    overlay — and the loader refuses to pretend otherwise.
    """
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    tenant_id: str
    app_id: str
    entry_point: Optional[str] = None
    anchors: dict[str, AnchorOverride] = Field(default_factory=dict, description="anchor_id → overrides")
    text_substitutions: dict[str, str] = Field(default_factory=dict, description="Literal text remaps applied to expectations/patterns")
    notes: list[str] = Field(default_factory=list)

    def apply(self, cap: Capability) -> Capability:
        if cap.app_id != self.app_id:
            raise ValueError(f"overlay for app {self.app_id!r} cannot be applied to capability of app {cap.app_id!r}")
        data = cap.model_dump(mode="json")
        if self.entry_point:
            data["entry_point"] = self.entry_point

        def patch_anchor(a: dict | None):
            if not a:
                return
            for k in ("name", "label", "near_text"):
                a[k] = subst(a.get(k))
            if a.get("table_cell"):
                for k in ("row_header", "row_header_column", "column_header"):
                    a["table_cell"][k] = subst(a["table_cell"].get(k))
            ov = self.anchors.get(a.get("anchor_id", ""))
            if ov:
                for k, v in ov.model_dump(exclude_none=True).items():
                    a[k] = v

        def subst(s: str | None):
            if not s:
                return s
            for k, v in self.text_substitutions.items():
                s = s.replace(k, v)
            return s

        def patch_expect(e: dict | None):
            if not e:
                return
            e["text_visible"] = subst(e.get("text_visible"))
            e["text_pattern"] = subst(e.get("text_pattern"))
            patch_anchor(e.get("role_visible"))

        def patch_steps(steps):
            for s in steps:
                patch_anchor(s.get("target"))
                patch_expect(s.get("expect"))
                if s.get("value") and not PARAM_RE.search(s["value"]):
                    s["value"] = subst(s["value"])

        patch_steps(data["steps"])
        for b in data["recovery_blocks"].values():
            patch_steps(b)
        patch_expect(data["checkpoint"]["expect"])
        for r in data["conditions"]:
            r["match"]["text_pattern"] = subst(r["match"].get("text_pattern"))
            patch_anchor(r["match"].get("anchor_present"))
            patch_anchor(r["response"].get("dismiss_target"))
        data["notes"] = data.get("notes", []) + [f"overlay applied: tenant={self.tenant_id}"]
        return Capability.model_validate(data)
