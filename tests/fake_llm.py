"""
A deterministic stand-in for the Anthropic client.

It is NOT an LLM and makes no decisions of its own: it plays back a fixed
script of tool calls, resolving refs by (role, name) against the screen it is
shown, so the discovery loop / recorder / artifact path can be tested
hermetically. The real discovery run (with a real model) lives in /evidence.
"""
from __future__ import annotations

import re
import types
import uuid


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeAnthropic:
    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.calls: list[list] = []
        self.models = types.SimpleNamespace(retrieve=lambda m: None, list=lambda limit=100: types.SimpleNamespace(data=[]))
        self.messages = types.SimpleNamespace(create=self._create)

    @staticmethod
    def _screen(messages) -> str:
        last = messages[-1]["content"]
        if isinstance(last, list):
            return "\n".join(c.get("content", "") for c in last if isinstance(c, dict))
        return last

    @staticmethod
    def _ref(screen: str, role: str, name: str, extra: str = "") -> str:
        pat = re.compile(r"\[(f\d+e\d+)\] " + re.escape(role) + r' "' + re.escape(name) + r'"' + (".*" + re.escape(extra) if extra else ""))
        m = pat.search(screen)
        if not m:
            raise AssertionError(f"fake model could not find {role} {name!r} {extra!r} on screen:\n{screen}")
        return m.group(1)

    def _create(self, model, max_tokens, system, tools, messages):
        self.calls.append(messages)
        screen = self._screen(messages)
        if not self.script:
            raise AssertionError("fake model script exhausted")
        step = self.script.pop(0)
        args = dict(step["args"])
        if "target" in step:
            args["ref"] = self._ref(screen, *step["target"])
        if "dismiss_target" in step:
            args["dismiss_ref"] = self._ref(screen, *step["dismiss_target"])
        blocks = [_Block(type="text", text=step.get("say", "")),
                  _Block(type="tool_use", id="toolu_" + uuid.uuid4().hex[:8], name=step["tool"], input=args)]
        return _Block(content=blocks, stop_reason="tool_use", usage=_Block(input_tokens=10, output_tokens=5))


def savings_balance_script() -> list[dict]:
    return [
        {"tool": "type", "target": ("textbox", "Operator ID"), "args": {"text": "${secret:MERIDIAN_DEMO_USER}", "reason": "enter operator id"}},
        {"tool": "type", "target": ("textbox", "Password"), "args": {"text": "${secret:MERIDIAN_DEMO_PASS}", "reason": "enter password"}},
        {"tool": "click", "target": ("button", "Sign On"), "args": {"reason": "sign on"}},
        {"tool": "click", "target": ("link", "Member Lookup"), "args": {"reason": "open member lookup"}},
        {"tool": "type", "target": ("textbox", "Member Number"), "args": {"text": "${member_id}", "reason": "enter member number"}},
        {"tool": "click", "target": ("button", "Search"), "args": {"reason": "search for member"}},
        {"tool": "read", "target": ("cell", "Dana R. Whitfield"), "args": {"output_name": "member_name", "description": "Member full name"}},
        {"tool": "read", "target": ("cell", "$4,812.37"), "args": {"output_name": "savings_balance", "description": "Current regular savings balance"}},
        {"tool": "done", "args": {"checkpoint_text": "Current Balance", "capability_id": "meridian_core.member_savings_balance",
                                  "title": "Read member savings balance",
                                  "description": "Look up a member by number and return their regular savings balance."}},
    ]


def not_found_script() -> list[dict]:
    return [
        {"tool": "type", "target": ("textbox", "Operator ID"), "args": {"text": "${secret:MERIDIAN_DEMO_USER}", "reason": "enter operator id"}},
        {"tool": "type", "target": ("textbox", "Password"), "args": {"text": "${secret:MERIDIAN_DEMO_PASS}", "reason": "enter password"}},
        {"tool": "click", "target": ("button", "Sign On"), "args": {"reason": "sign on"}},
        {"tool": "click", "target": ("link", "Member Lookup"), "args": {"reason": "open member lookup"}},
        {"tool": "type", "target": ("textbox", "Member Number"), "args": {"text": "${member_id}", "reason": "enter member number"}},
        {"tool": "click", "target": ("button", "Search"), "args": {"reason": "search for member"}},
        {"tool": "declare_condition", "args": {"kind": "business_outcome", "text_pattern": r"No record found for Member Number \d+",
                                               "outcome_code": "MEMBER_NOT_FOUND", "description": "No such member"}},
        {"tool": "stuck", "args": {"reason": "BUSINESS_OUTCOME: MEMBER_NOT_FOUND"}},
    ]
