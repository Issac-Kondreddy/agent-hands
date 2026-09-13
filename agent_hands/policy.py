"""
Safety guardrails: allowlist, risk handling, redaction.

The policy is *configuration*, loaded from a JSON/YAML-ish dict, and it is
enforced in exactly one place — `Policy.check()` — which both the discovery
agent and the replay engine call before every action. Neither of them can
act without passing through it. That single choke point is the argument for
why "the agent must not act outside the allowlist" actually holds.

Three concerns, kept separate:

  * **Where** may we act?   → host/route allowlist (web) / app allowlist (desktop)
  * **What** may we do?     → allowed action types, and how each risk class is handled
  * **What may we remember?** → redaction of secrets and PII in artifacts & evidence
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

from .schema import ActionType, RiskClass

RiskHandling = Literal["allow", "require_human", "block"]


class PolicyViolation(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass
class Policy:
    allowed_hosts: list[str] = field(default_factory=list)          # exact host[:port] or '*.example.com'
    allowed_path_prefixes: list[str] = field(default_factory=lambda: ["/"])
    denied_path_patterns: list[str] = field(default_factory=list)   # regexes, e.g. '/admin/.*', '.*/logout'
    allowed_actions: list[str] = field(default_factory=lambda: [a.value for a in ActionType])
    risk_handling: dict[str, RiskHandling] = field(default_factory=lambda: {
        RiskClass.READ_ONLY.value: "allow",
        RiskClass.REVERSIBLE.value: "allow",
        RiskClass.IRREVERSIBLE.value: "require_human",
    })
    irreversible_control_patterns: list[str] = field(default_factory=lambda: [
        r"\b(confirm|finali[sz]e|submit|post|approve|authori[sz]e|transfer|pay|disburse|close account|delete|reverse)\b"
    ])
    redact_patterns: dict[str, str] = field(default_factory=lambda: {
        "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
        "card": r"\b(?:\d[ -]?){13,19}\b",
        "account_no": r"\b[A-Z]-\d{7}-\d{2}\b",
        "email": r"[\w.+-]+@[\w-]+\.[\w.]+",
        "secret_kv": r"(?i)(password|passwd|pwd|token|secret|api[_-]?key)\s*[:=]\s*\S+",
    })
    max_steps: int = 40
    step_timeout_ms: int = 15_000
    run_timeout_s: int = 300

    # ------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        data = json.loads(Path(path).read_text())
        return cls(**data)

    @classmethod
    def for_local_target(cls, host: str = "127.0.0.1:5055") -> "Policy":
        return cls(allowed_hosts=[host], allowed_path_prefixes=["/t/"],
                   denied_path_patterns=[r".*/logout$", r".*/__chaos$"])

    # ------------------------------------------------------------ checks
    def host_allowed(self, url: str) -> bool:
        u = urlparse(url)
        host = u.netloc
        if not host:
            return False
        for pat in self.allowed_hosts:
            if pat == host:
                return True
            if pat.startswith("*.") and host.endswith(pat[1:]):
                return True
        return False

    def path_allowed(self, url: str) -> bool:
        path = urlparse(url).path or "/"
        if not any(path.startswith(p) for p in self.allowed_path_prefixes):
            return False
        return not any(re.fullmatch(p, path) for p in self.denied_path_patterns)

    def classify_control(self, control_name: str, declared: RiskClass) -> RiskClass:
        """A step's declared risk can only be *raised* by the policy, never lowered."""
        if declared == RiskClass.IRREVERSIBLE:
            return declared
        for pat in self.irreversible_control_patterns:
            if re.search(pat, control_name or "", re.I):
                return RiskClass.IRREVERSIBLE
        return declared

    def check(self, action: ActionType, *, url: Optional[str] = None, control_name: str = "",
              declared_risk: RiskClass = RiskClass.READ_ONLY, human_approved: bool = False) -> RiskClass:
        """
        Gate one action. Returns the effective risk class, or raises PolicyViolation.

        `human_approved=True` means a human has *explicitly* approved this
        specific step in this run (via the handoff seam) — it converts
        'require_human' into 'allow' for this call only.
        """
        if action.value not in self.allowed_actions:
            raise PolicyViolation("ACTION_NOT_ALLOWED", f"action {action.value!r} is not permitted by policy")
        if url is not None:
            if not self.host_allowed(url):
                raise PolicyViolation("HOST_NOT_ALLOWED", f"{urlparse(url).netloc!r} is outside the allowlist")
            if not self.path_allowed(url):
                raise PolicyViolation("ROUTE_NOT_ALLOWED", f"route {urlparse(url).path!r} is denied by policy")
        risk = self.classify_control(control_name, declared_risk)
        handling = self.risk_handling.get(risk.value, "block")
        if handling == "block":
            raise PolicyViolation("RISK_BLOCKED", f"{risk.value} action on {control_name!r} is blocked by policy")
        if handling == "require_human" and not human_approved:
            raise PolicyViolation("HUMAN_APPROVAL_REQUIRED",
                                  f"{risk.value} action on {control_name!r} requires a human decision")
        return risk

    # ------------------------------------------------------------ redaction
    def redactor(self, sensitive_values: dict[str, str] | None = None) -> "Redactor":
        return Redactor(self.redact_patterns, sensitive_values or {})


class Redactor:
    """Scrubs secrets/PII from anything that gets persisted. Patterns first, then known values (placeholders are inert to the patterns)."""

    def __init__(self, patterns: dict[str, str], sensitive_values: dict[str, str]):
        self._patterns = [(k, re.compile(v)) for k, v in patterns.items()]
        # longest first so partial overlaps don't leak suffixes
        self._values = sorted(((k, v) for k, v in sensitive_values.items() if v), key=lambda kv: -len(kv[1]))

    def __call__(self, text: Optional[str]) -> str:
        if not text:
            return text or ""
        for name, pat in self._patterns:
            text = pat.sub(f"[REDACTED {name}]", text)
        for name, val in self._values:
            text = text.replace(val, f"[REDACTED {name}]")
        return text

    def dict(self, d: dict) -> dict:
        return {k: (self(v) if isinstance(v, str) else self.dict(v) if isinstance(v, dict)
                    else [self(x) if isinstance(x, str) else x for x in v] if isinstance(v, list) else v)
                for k, v in d.items()}
