"""
The locator ladder: resolve an `Anchor` against an `Observation`.

Rungs, most stable first. Replay records which rung resolved each step,
so a run that "worked" but slid down the ladder is visible as drift.

    1. exact      role + accessible name (normalized)          confidence 1.00
    2. label      role + associated/adjacent label             0.95
    3. table      row header × column header                   0.95
    4. fuzzy      role + name/label containment                0.80
    5. near       role + distinctive nearby text (geometry)    0.60
    6. geometric  recorded bounding box (opt-in per anchor)    0.30

Ambiguity is an error, not a coin toss: if a rung yields several candidates
and the remaining anchor fields cannot separate them, we stop. In a bank
back office, clicking the *wrong* "Confirm" is worse than clicking nothing.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

from .schema import Anchor
from .surface.base import Element, Observation


class LocatorError(Exception):
    pass


class NotFound(LocatorError):
    pass


class Ambiguous(LocatorError):
    def __init__(self, anchor: Anchor, candidates: list[Element], rung: str):
        self.candidates = candidates
        super().__init__(f"anchor {anchor.anchor_id!r} matched {len(candidates)} elements at rung {rung!r}: "
                         + "; ".join(c.describe() for c in candidates[:5]))


@dataclass
class Resolution:
    element: Optional[Element]        # None only for pure-geometric fallback
    rung: str
    confidence: float
    geometric_point: Optional[tuple[list[str], float, float]] = None   # (frame, x, y) when element is None

    @property
    def drifted(self) -> bool:
        return self.rung not in {"exact", "label", "table"}


_PUNCT = re.compile(r"[\s:*_\-–—.,;!?()\[\]\"']+")


def norm(s: Optional[str]) -> str:
    return _PUNCT.sub(" ", (s or "").lower()).strip()


def _frame_ok(anchor: Anchor, e: Element) -> bool:
    # If the anchor names a frame path, honour it; if not, any frame is fine.
    return not anchor.frame or e.frame == anchor.frame


def _role_ok(anchor: Anchor, e: Element) -> bool:
    if not anchor.role:
        return True
    if anchor.role == e.role:
        return True
    # a submit <input> and a <button> are both "button"; links styled as buttons are common in legacy apps
    return {anchor.role, e.role} == {"button", "link"}


def _center(e: Element) -> tuple[float, float]:
    x, y, w, h = e.bbox
    return (x + w / 2, y + h / 2)


def _dist(a: Element, b: Element) -> float:
    ax, ay = _center(a)
    bx, by = _center(b)
    return math.hypot(ax - bx, ay - by)


def _iou(a: tuple, b: tuple) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _disambiguate(anchor: Anchor, cands: list[Element], obs: Observation) -> list[Element]:
    """Use the anchor's remaining fields to thin a candidate list."""
    if len(cands) <= 1:
        return cands
    if anchor.label:
        n = norm(anchor.label)
        thin = [c for c in cands if norm(c.label) == n or norm(c.name) == n]
        if thin:
            cands = thin
    if len(cands) > 1 and anchor.near_text:
        texts = [t for t in obs.elements if norm(anchor.near_text) in norm(t.name)]
        if texts:
            cands = sorted(cands, key=lambda c: min(_dist(c, t) for t in texts))[:1]
    if len(cands) > 1 and anchor.bbox:
        cands = sorted(cands, key=lambda c: -_iou(c.bbox, tuple(anchor.bbox)))
        if _iou(cands[0].bbox, tuple(anchor.bbox)) > 0.5:
            cands = cands[:1]
    return cands


def resolve(anchor: Anchor, obs: Observation) -> Resolution:
    pool = [e for e in obs.elements if _frame_ok(anchor, e) and _role_ok(anchor, e) and not e.disabled]

    # 1. exact role + name
    if anchor.name:
        n = norm(anchor.name)
        cands = [e for e in pool if norm(e.name) == n]
        cands = _disambiguate(anchor, cands, obs)
        if len(cands) == 1:
            return Resolution(cands[0], "exact", 1.0)
        if len(cands) > 1:
            raise Ambiguous(anchor, cands, "exact")

    # 2. role + label
    if anchor.label:
        n = norm(anchor.label)
        cands = [e for e in pool if norm(e.label) == n or (e.role in {"textbox", "combobox"} and norm(e.name) == n)]
        cands = _disambiguate(anchor, cands, obs)
        if len(cands) == 1:
            return Resolution(cands[0], "label", 0.95)
        if len(cands) > 1:
            raise Ambiguous(anchor, cands, "label")

    # 3. table cell by headers
    if anchor.table_cell:
        tc = anchor.table_cell
        cands = [e for e in pool if e.table
                 and norm(e.table.row_header) == norm(tc.row_header)
                 and norm(e.table.column_header) == norm(tc.column_header)
                 and (not tc.row_header_column or norm(e.table.row_header_column) == norm(tc.row_header_column))]
        if len(cands) == 1:
            return Resolution(cands[0], "table", 0.95)
        if len(cands) > 1:
            raise Ambiguous(anchor, cands, "table")

    # 4. fuzzy containment on name/label
    probe = norm(anchor.name or anchor.label or "")
    if probe:
        cands = [e for e in pool if probe in norm(e.name) or probe in norm(e.label)
                 or (norm(e.name) and norm(e.name) in probe and len(norm(e.name)) >= 4)]
        cands = _disambiguate(anchor, cands, obs)
        if len(cands) == 1:
            return Resolution(cands[0], "fuzzy", 0.8)
        if len(cands) > 1:
            raise Ambiguous(anchor, cands, "fuzzy")

    # 5. nearest control to a distinctive text
    if anchor.near_text and anchor.role:
        texts = [t for t in obs.elements if norm(anchor.near_text) in norm(t.name) and _frame_ok(anchor, t)]
        if texts:
            ranked = sorted(pool, key=lambda c: min(_dist(c, t) for t in texts))
            if ranked and min(_dist(ranked[0], t) for t in texts) < 250:
                return Resolution(ranked[0], "near", 0.6)

    # 6. geometry
    if anchor.bbox and anchor.allow_geometric_fallback:
        best = max(pool, key=lambda e: _iou(e.bbox, tuple(anchor.bbox)), default=None)
        if best is not None and _iou(best.bbox, tuple(anchor.bbox)) > 0.3:
            return Resolution(best, "geometric", 0.3)
        x, y, w, h = anchor.bbox
        return Resolution(None, "geometric", 0.2, geometric_point=(anchor.frame, x + w / 2, y + h / 2))

    raise NotFound(f"anchor {anchor.anchor_id!r} ({anchor.role} {anchor.name or anchor.label or anchor.table_cell}) "
                   f"not found among {len(pool)} candidate elements")


def anchor_from_element(anchor_id: str, e: Element, rationale: str = "") -> Anchor:
    """Build a robust anchor from an observed element (used by the recorder)."""
    tc = None
    if e.role == "cell" and e.table and e.table.column_header and e.table.row_header:
        from .schema import TableCell
        tc = TableCell(row_header=e.table.row_header, row_header_column=e.table.row_header_column,
                       column_header=e.table.column_header)
    return Anchor(
        anchor_id=anchor_id, role=e.role, name=e.name or None,
        label=(e.label or None) if e.role in {"textbox", "combobox", "checkbox", "radio"} else None,
        table_cell=tc, frame=list(e.frame), bbox=[round(v, 1) for v in e.bbox],
        allow_geometric_fallback=False, rationale=rationale or None,
    )
