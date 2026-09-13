"""
Human-in-the-loop: detect stuck → route an intervention request → cede the
*same live session* to a person → take it back.

The control-transfer model is a small explicit state machine (`ControlLease`):

    AUTOMATION ──request──▶ INTERVENTION_REQUESTED ──take──▶ HUMAN
        ▲                                                     │
        └────────────── RESUMING ◀──────── release ───────────┘
                                          (or ABORTED)

Only the lease holder may act. The replay engine and the discovery agent
check `lease.holder` before every action; the `LiveSession` handed to the
operator checks it too. That is the answer to "how do you know who is (or
should be) in control": there is exactly one lease, and every action is
stamped with its holder in the evidence log.

The *operator surface* is pluggable behind the `Operator` protocol:

  * `ScriptedOperator` — a deterministic stand-in for a human (tests, unattended
    evidence generation). Documented mock; see REPORT.md §5.
  * `ConsoleOperator`  — a person at the terminal drives the same browser by ref.
  * `WebOperator`      — a minimal local HTTP page: screenshot + controls + Resume.

All three act through the same `LiveSession`, so what the human did is
recorded exactly like what the automation did — same log, same redaction.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Callable, Literal, Optional, Protocol

from .evidence import EvidenceLog
from .surface.base import Element, Observation, Surface


class LeaseState(str, Enum):
    AUTOMATION = "automation"
    INTERVENTION_REQUESTED = "intervention_requested"
    HUMAN = "human"
    RESUMING = "resuming"
    ABORTED = "aborted"


class NotLeaseHolder(RuntimeError):
    pass


class ControlLease:
    _transitions = {
        LeaseState.AUTOMATION: {LeaseState.INTERVENTION_REQUESTED},
        LeaseState.INTERVENTION_REQUESTED: {LeaseState.HUMAN, LeaseState.ABORTED},
        LeaseState.HUMAN: {LeaseState.RESUMING, LeaseState.ABORTED},
        LeaseState.RESUMING: {LeaseState.AUTOMATION},
        LeaseState.ABORTED: set(),
    }

    def __init__(self, log: EvidenceLog):
        self.state = LeaseState.AUTOMATION
        self.holder = "automation"
        self._log = log
        self._lock = threading.Lock()

    def _to(self, new: LeaseState, holder: str, reason: str = ""):
        with self._lock:
            if new not in self._transitions[self.state]:
                raise RuntimeError(f"illegal lease transition {self.state.value} → {new.value}")
            self.state = new
            self.holder = holder
            self._log.set_controller(holder, reason=f"{new.value}: {reason}")

    def request_intervention(self, reason: str):
        self._to(LeaseState.INTERVENTION_REQUESTED, "automation(paused)", reason)

    def take(self, human_id: str):
        self._to(LeaseState.HUMAN, f"human:{human_id}", "operator took control")

    def release(self, human_id: str, note: str = ""):
        self._to(LeaseState.RESUMING, "automation(resuming)", f"operator {human_id} released control: {note}")
        self._to(LeaseState.AUTOMATION, "automation", "resumed")

    def abort(self, who: str, note: str = ""):
        self._to(LeaseState.ABORTED, who, note)

    def assert_holder(self, who: str):
        if self.holder != who:
            raise NotLeaseHolder(f"{who!r} tried to act but lease is held by {self.holder!r} ({self.state.value})")


# ---------------------------------------------------------------------------
# Intervention request & decision
# ---------------------------------------------------------------------------
@dataclass
class InterventionRequest:
    request_id: str
    run_id: str
    capability_id: str
    goal: str
    step_id: Optional[str]
    step_description: str
    reason_code: str            # STUCK | AMBIGUOUS_TARGET | HUMAN_APPROVAL_REQUIRED | UNRECOVERABLE | POLICY
    reason: str
    observation_text: str
    screenshot: Optional[str]
    location: str
    allowed_decisions: list[str] = field(default_factory=lambda: ["resume", "approve_step", "skip_step", "abort"])
    created_at: float = field(default_factory=time.time)

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("observation_text")
        return d


DecisionKind = Literal["resume", "approve_step", "skip_step", "abort"]


@dataclass
class Decision:
    kind: DecisionKind
    note: str = ""
    human_id: str = "operator"


# ---------------------------------------------------------------------------
# The live session handed to the human
# ---------------------------------------------------------------------------
class LiveSession:
    """
    A thin, *lease-checked* view of the very same Surface the automation is
    using. Every human action is logged with controller=human:<id>.
    """

    def __init__(self, surface: Surface, lease: ControlLease, log: EvidenceLog, human_id: str,
                 on_action: Optional[Callable[[str, Optional["Element"], Optional[str], Observation], None]] = None):
        self._s, self._lease, self._log, self._who = surface, lease, log, f"human:{human_id}"
        self.last_observation: Optional[Observation] = None
        self._on_action = on_action   # discovery uses this to record human steps into the artifact

    def _notify(self, action: str, ref: Optional[str], value: Optional[str]):
        if self._on_action is None:
            return
        el = self.last_observation.find(ref) if (ref and self.last_observation) else None
        before = self.last_observation
        self._on_action(action, el, value, before)

    def _guard(self):
        self._lease.assert_holder(self._who)

    @property
    def evidence_root(self):
        return self._log.root

    def observe(self) -> Observation:
        self._guard()
        self.last_observation = self._s.observe()
        return self.last_observation

    def click(self, ref: str):
        self._guard()
        el = self.last_observation.find(ref) if self.last_observation else None
        self._log.event("human.action", action="click", ref=ref, target=el.describe() if el else None)
        self._s.click(ref)
        self._notify("click", ref, None)

    def type(self, ref: str, text: str):
        self._guard()
        el = self.last_observation.find(ref) if self.last_observation else None
        secret = bool(el and el.input_type == "password")
        self._log.event("human.action", action="type", ref=ref, target=el.describe() if el else None,
                        value="<password:REDACTED>" if secret else text)
        self._s.type(ref, text)
        self._notify("type", ref, text)

    def select(self, ref: str, value: str):
        self._guard()
        self._log.event("human.action", action="select", ref=ref, value=value)
        self._s.select(ref, value)
        self._notify("select", ref, value)

    def press(self, key: str):
        self._guard()
        self._log.event("human.action", action="press", key=key)
        self._s.press(key)

    def navigate(self, url: str):
        self._guard()
        self._log.event("human.action", action="navigate", url=url)
        self._s.navigate(url)

    def screenshot(self, label: str) -> str:
        self._guard()
        return self._log.snapshot(self._s, label)["screenshot"]


class Operator(Protocol):
    def handle(self, request: InterventionRequest, session: LiveSession) -> Decision: ...


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------
class ScriptedOperator:
    """
    Deterministic stand-in for a person. `script` is a list of
    ("click"|"type"|"select"|"press"|"navigate", *args) tuples where a
    target is given as a predicate over Elements *or* a (role, name) pair.
    Ends with `decision`. Used by tests and by the evidence generator.
    """

    def __init__(self, script: list[tuple], decision: Decision, human_id: str = "scripted-operator"):
        self.script, self.decision, self.human_id = script, decision, human_id
        self.handled: list[InterventionRequest] = []

    def _find(self, obs: Observation, target):
        if callable(target):
            matches = [e for e in obs.elements if target(e)]
        else:
            role, name = target
            matches = [e for e in obs.elements if e.role == role and e.name.strip().lower() == name.lower()]
        if not matches:
            raise RuntimeError(f"scripted operator could not find {target}")
        return matches[0].ref

    def handle(self, request: InterventionRequest, session: LiveSession) -> Decision:
        self.handled.append(request)
        for act in self.script:
            obs = session.observe()
            kind = act[0]
            if kind == "click":
                session.click(self._find(obs, act[1]))
            elif kind == "type":
                session.type(self._find(obs, act[1]), act[2])
            elif kind == "select":
                session.select(self._find(obs, act[1]), act[2])
            elif kind == "press":
                session.press(act[1])
            elif kind == "navigate":
                session.navigate(act[1])
        return Decision(kind=self.decision.kind, note=self.decision.note, human_id=self.human_id)


class ConsoleOperator:
    """A human at the terminal. Types commands like `click f3e5`, `type f3e2 12345`, `resume`."""

    def __init__(self, human_id: str = "console-operator", input_fn=input, print_fn=print):
        self.human_id, self._in, self._out = human_id, input_fn, print_fn

    def handle(self, request: InterventionRequest, session: LiveSession) -> Decision:
        self._out("\n=== INTERVENTION REQUESTED ===")
        self._out(f"capability: {request.capability_id}   step: {request.step_id} — {request.step_description}")
        self._out(f"reason: [{request.reason_code}] {request.reason}")
        self._out(f"screenshot: {request.screenshot}")
        self._out("You now hold the live session. Commands: observe | click <ref> | type <ref> <text> | "
                  "select <ref> <opt> | press <key> | approve_step | skip_step | resume | abort")
        while True:
            try:
                line = self._in("operator> ").strip()
            except EOFError:
                return Decision("abort", "eof", self.human_id)
            if not line:
                continue
            cmd, *rest = line.split(" ", 2)
            try:
                if cmd == "observe":
                    self._out(session.observe().render())
                elif cmd == "click":
                    session.click(rest[0]); self._out(session.observe().render())
                elif cmd == "type":
                    session.type(rest[0], rest[1] if len(rest) > 1 else "")
                elif cmd == "select":
                    session.select(rest[0], rest[1])
                elif cmd == "press":
                    session.press(rest[0])
                elif cmd in {"approve_step", "skip_step", "resume", "abort"}:
                    return Decision(cmd, " ".join(rest), self.human_id)  # type: ignore[arg-type]
                else:
                    self._out("unknown command")
            except Exception as e:  # noqa: BLE001 — surface any error to the operator, keep the session
                self._out(f"error: {e}")
