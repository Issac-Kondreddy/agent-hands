"""
Evidence: the structured, redacted record of a run.

Every run — discovery, replay, or human intervention — writes to one
directory:

    runs/<run_id>/
        run.jsonl          one JSON event per line (what, why, when, who was in control)
        step-03.png        screenshot after each step / on failure
        step-03.obs.txt    a11y snapshot rendering at that moment (the "DOM snapshot" analogue)
        result.json        the final structured result

Two rules make this safe for regulated data:
  * everything passes through the Redactor before it is written
  * parameter *values* flagged sensitive are never written at all; typed
    text into password controls is never logged, even during discovery
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .policy import Redactor


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class EvidenceLog:
    root: Path
    run_id: str
    kind: str                                  # discovery | replay | intervention
    redact: Redactor
    controller: str = "automation"             # who is in control right now (automation | human:<id>)
    _t0: float = field(default_factory=time.time)
    _seq: int = 0

    @classmethod
    def start(cls, base_dir: str | Path, kind: str, redact: Redactor, run_id: Optional[str] = None,
              meta: Optional[dict] = None) -> "EvidenceLog":
        run_id = run_id or f"{kind}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        root = Path(base_dir) / run_id
        root.mkdir(parents=True, exist_ok=True)
        log = cls(root=root, run_id=run_id, kind=kind, redact=redact)
        log.event("run.start", kind=kind, **(meta or {}))
        return log

    @property
    def path(self) -> Path:
        return self.root / "run.jsonl"

    def event(self, type_: str, **fields: Any) -> dict:
        self._seq += 1
        rec = {"seq": self._seq, "ts": _now(), "t_ms": int((time.time() - self._t0) * 1000),
               "controller": self.controller, "type": type_}
        rec.update(self.redact.dict(fields))
        with self.path.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def snapshot(self, surface, label: str, observation=None) -> dict:
        """Richer failure/debug signal: screenshot + a11y rendering."""
        png = self.root / f"{label}.png"
        surface.screenshot(str(png))
        paths = {"screenshot": png.name}
        if observation is not None:
            txt = self.root / f"{label}.obs.txt"
            txt.write_text(self.redact(observation.render(max_elements=400)))
            paths["observation"] = txt.name
        return paths

    def set_controller(self, who: str, reason: str = ""):
        prev, self.controller = self.controller, who
        self.event("control.transfer", **{"from": prev, "to": who, "reason": reason})

    def finish(self, result: dict) -> Path:
        out = self.root / "result.json"
        out.write_text(json.dumps(self.redact.dict(result), indent=2, ensure_ascii=False))
        self.event("run.end", status=result.get("status"))
        return out
