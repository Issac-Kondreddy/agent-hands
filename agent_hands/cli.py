"""
agent-hands CLI.

    python -m agent_hands discover  --goal "..." --param member_id=12345 [--enrich artifacts/x.v1.json]
    python -m agent_hands replay    --capability meridian_core.member_savings_balance --param member_id=23456
    python -m agent_hands replay    ... --tenant harbor          (apply overrides/harbor.json)
    python -m agent_hands replay    ... --operator web|console   (real human handoff)
    python -m agent_hands replay    ... --chaos interstitial_once=1 --chaos expire_session_in=3
    python -m agent_hands approve   --capability ... --version 1 --reviewer isaac
    python -m agent_hands catalog                                (agent-facing tool list)
    python -m agent_hands stability --capability ... --param ... --runs 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .catalog import Catalog
from .discovery.agent import DEFAULT_MODEL, DiscoveryAgent
from .discovery.recorder import AppProfile
from .policy import Policy
from .schema import Capability, Parameter, ParamType
from .surface.playwright_surface import PlaywrightSurface

DEFAULT_BASE = os.environ.get("MERIDIAN_BASE", "http://127.0.0.1:5055")
SECRETS = ["MERIDIAN_DEMO_USER", "MERIDIAN_DEMO_PASS"]


def _params(items: list[str]) -> dict[str, str]:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k.strip()] = v
    return out


def _policy(args) -> Policy:
    if args.policy:
        return Policy.load(args.policy)
    host = DEFAULT_BASE.split("//")[1]
    return Policy.for_local_target(host)


def _operator(kind: str | None):
    if kind == "web":
        from .operator_web import WebOperator
        return WebOperator(open_browser=False)
    if kind == "console":
        from .handoff import ConsoleOperator
        return ConsoleOperator()
    if kind == "auto-approve":
        # DEV ONLY: a scripted operator that approves irreversible steps. Documented in README; never for production.
        from .handoff import Decision, ScriptedOperator
        return ScriptedOperator([], Decision("approve_step", "auto-approved (dev flag)"), human_id="dev-auto-approver")
    return None


def _apply_chaos(surface: PlaywrightSurface, flags: list[str]):
    if not flags:
        return
    surface.page.goto(DEFAULT_BASE + "/__health")
    body = "&".join(flags)
    surface.page.evaluate("b => fetch('/__chaos', {method:'POST', body:b, headers:{'Content-Type':'application/x-www-form-urlencoded'}}).then(r=>r.text())", body)
    print(f"chaos injected: {body}")


def cmd_discover(args):
    policy = _policy(args)
    values = _params(args.param)
    params = [Parameter(name=k, description=args.param_desc.get(k, "") if args.param_desc else "",
                        pattern=(r"\d{5}" if k == "member_id" else None)) for k in values]
    profile = AppProfile.load(args.profile) if args.profile and Path(args.profile).exists() else None
    surface = PlaywrightSurface(headless=not args.headed)
    try:
        enrich = Capability.from_json(Path(args.enrich).read_text()) if args.enrich else None
        agent = DiscoveryAgent(surface, policy, app_id=args.app_id, entry_point=args.entry or f"{DEFAULT_BASE}/t/{args.tenant}/",
                               parameters=params, param_values=values, secret_names=SECRETS, profile=profile,
                               tenant=args.tenant, model=args.model, evidence_dir=args.evidence_dir,
                               artifacts_dir=args.artifacts_dir, operator=_operator(args.operator), max_steps=args.max_steps)
        r = agent.run(args.goal, enrich=enrich)
    finally:
        surface.close()
    print(json.dumps({"status": r.status, "detail": r.detail, "artifact": r.artifact_path, "evidence": r.evidence_dir,
                      "model_calls": r.model_calls, "actions": r.actions, "duration_ms": r.duration_ms}, indent=2))
    return 0 if r.status in {"SUCCESS", "ENRICHED"} else 1


def cmd_replay(args):
    policy = _policy(args)
    catalog = Catalog(args.artifacts_dir)
    surface = PlaywrightSurface(headless=not args.headed)
    try:
        _apply_chaos(surface, args.chaos)
        r = catalog.invoke(args.capability, _params(args.param), surface=surface, policy=policy, tenant=args.tenant,
                           operator=_operator(args.operator), evidence_dir=args.evidence_dir,
                           approved_only=not args.allow_draft, version=args.version)
    finally:
        surface.close()
    print(json.dumps(r.to_dict(), indent=2))
    return 0 if r.status in {"SUCCESS", "BUSINESS_OUTCOME"} else 2


def cmd_approve(args):
    p = Catalog(args.artifacts_dir).approve(args.capability, args.version, args.reviewer)
    print(f"approved → {p}")
    return 0


def cmd_catalog(args):
    print(json.dumps(Catalog(args.artifacts_dir).list_tools(approved_only=not args.allow_draft), indent=2))
    return 0


def cmd_stability(args):
    policy = _policy(args)
    catalog = Catalog(args.artifacts_dir)
    results = []
    for i in range(args.runs):
        surface = PlaywrightSurface(headless=True)
        try:
            r = catalog.invoke(args.capability, _params(args.param), surface=surface, policy=policy, tenant=args.tenant,
                               evidence_dir=args.evidence_dir, approved_only=not args.allow_draft)
        finally:
            surface.close()
        results.append(r)
        print(f"run {i+1}: {r.status} {r.outcome_code or ''} outputs={r.outputs} drift={r.drift} {r.duration_ms}ms")
    ok = [r for r in results if r.status == "SUCCESS"]
    distinct = {json.dumps(r.outputs, sort_keys=True) for r in ok}
    report = {"runs": args.runs, "success": len(ok), "success_rate": len(ok) / args.runs, "distinct_outputs": len(distinct),
              "drift_runs": sum(1 for r in results if r.drift),
              "mean_ms": int(sum(r.duration_ms for r in results) / args.runs)}
    print(json.dumps(report, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agent-hands")
    ap.add_argument("--artifacts-dir", default="artifacts")
    ap.add_argument("--evidence-dir", default="evidence/runs")
    ap.add_argument("--policy", help="JSON policy file (default: local Meridian allowlist)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover")
    d.add_argument("--goal", required=True)
    d.add_argument("--param", action="append", default=[], help="name=value (example value for the run)")
    d.add_argument("--param-desc", type=json.loads, default=None, help='JSON {"member_id": "5-digit member number"}')
    d.add_argument("--app-id", default="meridian_core")
    d.add_argument("--tenant", default="meridian")
    d.add_argument("--entry", help="entry URL (default: {base}/t/{tenant}/)")
    d.add_argument("--profile", default="profiles/meridian_core.json")
    d.add_argument("--model", default=DEFAULT_MODEL)
    d.add_argument("--max-steps", type=int, default=25)
    d.add_argument("--enrich", help="existing artifact to teach a new condition/outcome")
    d.add_argument("--operator", choices=["web", "console", "auto-approve"])
    d.add_argument("--headed", action="store_true")
    d.set_defaults(fn=cmd_discover)

    r = sub.add_parser("replay")
    r.add_argument("--capability", required=True)
    r.add_argument("--version", type=int)
    r.add_argument("--param", action="append", default=[])
    r.add_argument("--tenant")
    r.add_argument("--operator", choices=["web", "console", "auto-approve"])
    r.add_argument("--chaos", action="append", default=[], help="inject: interstitial_once=1 | slow_ms=1500 | expire_session_in=3 | fail_next=1")
    r.add_argument("--allow-draft", action="store_true", help="replay an unapproved artifact (dev)")
    r.add_argument("--headed", action="store_true")
    r.set_defaults(fn=cmd_replay)

    a = sub.add_parser("approve")
    a.add_argument("--capability", required=True)
    a.add_argument("--version", type=int, required=True)
    a.add_argument("--reviewer", required=True)
    a.set_defaults(fn=cmd_approve)

    c = sub.add_parser("catalog")
    c.add_argument("--allow-draft", action="store_true")
    c.set_defaults(fn=cmd_catalog)

    s = sub.add_parser("stability")
    s.add_argument("--capability", required=True)
    s.add_argument("--param", action="append", default=[])
    s.add_argument("--tenant")
    s.add_argument("--runs", type=int, default=5)
    s.add_argument("--allow-draft", action="store_true")
    s.set_defaults(fn=cmd_stability)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
