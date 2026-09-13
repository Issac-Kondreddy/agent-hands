"""
The capability catalog — the agent-facing surface (stretch goal §8.1).

An AI agent does not read artifacts; it reads a *catalog* of callable
functions: name, description, typed parameters, typed returns, possible
outcomes, risk. `invoke()` is what the agent calls. Behind it: load the
approved artifact, apply the tenant overlay if any, replay deterministically,
return the structured result.

Only `approved` artifacts are invocable unattended. Drafts (fresh from
discovery) must be reviewed and promoted first — the approval gate is the
difference between "a model did this once" and "we trust this to run
unattended on a bank system".
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .policy import Policy
from .replay import ReplayEngine, ReplayResult
from .schema import Capability, TenantOverlay


class Catalog:
    def __init__(self, artifacts_dir: str | Path = "artifacts", overlays_dir: str | Path = "overrides"):
        self.artifacts_dir, self.overlays_dir = Path(artifacts_dir), Path(overlays_dir)

    # ------------------------------------------------------------ discovery
    def _load_all(self) -> list[Capability]:
        caps = []
        for p in sorted(self.artifacts_dir.glob("*.json")):
            try:
                caps.append(Capability.from_json(p.read_text()))
            except Exception as e:  # noqa: BLE001 — a bad file must not hide the good ones
                print(f"skipping {p}: {e}")
        return caps

    def get(self, capability_id: str, version: Optional[int] = None, approved_only: bool = True) -> Capability:
        cands = [c for c in self._load_all() if c.capability_id == capability_id
                 and (version is None or c.version == version)
                 and (not approved_only or c.status == "approved")]
        if not cands:
            raise KeyError(f"no {'approved ' if approved_only else ''}capability {capability_id!r}"
                           + (f" v{version}" if version else ""))
        return max(cands, key=lambda c: c.version)

    def list_tools(self, approved_only: bool = True) -> list[dict]:
        """Function-calling style descriptors — drop these straight into an agent's tool list."""
        latest: dict[str, Capability] = {}
        for c in self._load_all():
            if approved_only and c.status != "approved":
                continue
            if c.capability_id not in latest or c.version > latest[c.capability_id].version:
                latest[c.capability_id] = c
        out = []
        for c in latest.values():
            contract = c.agent_facing_contract()
            out.append({
                "name": contract["name"].replace(".", "__"),
                "description": f"{contract['description']} Possible outcomes: {', '.join(contract['possible_outcomes']) or 'none'}. "
                               f"Risk: {contract['max_risk']}.",
                "input_schema": {"type": "object",
                                 "properties": {k: {"type": "string", "description": v["description"],
                                                    **({"pattern": v["pattern"]} if v.get("pattern") else {})}
                                                for k, v in contract["parameters"].items()},
                                 "required": [k for k, v in contract["parameters"].items() if v["required"]]},
                "returns": contract["returns"],
                "version": contract["version"],
            })
        return out

    def overlay_for(self, tenant_id: str, app_id: str) -> Optional[TenantOverlay]:
        p = self.overlays_dir / f"{tenant_id}.json"
        if not p.exists():
            return None
        ov = TenantOverlay.model_validate_json(p.read_text())
        return ov if ov.app_id == app_id else None

    # ------------------------------------------------------------ approval
    def approve(self, capability_id: str, version: int, reviewer: str) -> Path:
        cap = self.get(capability_id, version, approved_only=False)
        cap.status = "approved"
        cap.notes.append(f"approved v{version} by {reviewer}")
        p = self.artifacts_dir / f"{cap.capability_id}.v{cap.version}.json"
        p.write_text(cap.to_json())
        return p

    # ------------------------------------------------------------ invoke
    def invoke(self, capability_id: str, params: dict, *, surface, policy: Policy, tenant: Optional[str] = None,
               operator=None, evidence_dir: str = "evidence/runs", approved_only: bool = True,
               version: Optional[int] = None) -> ReplayResult:
        cap = self.get(capability_id, version, approved_only=approved_only)
        if tenant and tenant != cap.recorded_on_tenant:
            ov = self.overlay_for(tenant, cap.app_id)
            if ov is None:
                raise KeyError(f"no overlay for tenant {tenant!r} on app {cap.app_id!r}; record one or add overrides/{tenant}.json")
            cap = ov.apply(cap)
        engine = ReplayEngine(surface, policy, evidence_dir=evidence_dir, operator=operator, goal=cap.title)
        return engine.run(cap, params)
