"""
Playwright implementation of the Surface protocol.

Perception is an *accessibility-style* snapshot that we compute ourselves,
frame by frame, rather than CSS/XPath querying:

  * roles are derived from semantics (tag + type + aria), never from classes
  * names/labels follow what a human sees — including the legacy pattern of
    "label in the previous table cell"
  * table cells carry their row/column *header text*
  * geometry is captured so the geometric fallback (and a future
    screenshot-only surface) has something to work with

We never touch ids, data-testids or class names. The target app has none,
and neither do the apps this system is meant for.
"""
from __future__ import annotations

import time
from typing import Optional

from playwright.sync_api import Browser, Frame, Page, Playwright, sync_playwright, Error as PWError

from .base import Element, Observation, SurfaceError, TableInfo

_SNAPSHOT_JS = r"""
() => {
  const refs = [];
  window.__ah_refs = refs;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const roleOf = el => {
    const t = el.tagName.toLowerCase();
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    if (t === 'a' && el.hasAttribute('href')) return 'link';
    if (t === 'button') return 'button';
    if (t === 'input') {
      const ty = (el.type || 'text').toLowerCase();
      if (['button','submit','reset','image'].includes(ty)) return 'button';
      if (ty === 'checkbox') return 'checkbox';
      if (ty === 'radio') return 'radio';
      if (ty === 'hidden') return null;
      return 'textbox';
    }
    if (t === 'textarea') return 'textbox';
    if (t === 'select') return 'combobox';
    if (/^h[1-6]$/.test(t)) return 'heading';
    if (t === 'td' || t === 'th') return 'cell';
    if (t === 'img' && el.alt) return 'img';
    return null;
  };
  const labelFor = el => {
    // 1. aria-label / aria-labelledby
    if (el.getAttribute('aria-label')) return norm(el.getAttribute('aria-label'));
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = document.getElementById(lb); if (t) return norm(t.innerText); }
    // 2. <label for=...> or wrapping <label>
    if (el.id) { const l = document.querySelector(`label[for="${el.id}"]`); if (l) return norm(l.innerText); }
    const wrap = el.closest('label'); if (wrap) return norm(wrap.innerText.replace(el.value || '', ''));
    // 3. legacy: text of the previous <td> in the same row, or preceding text in the same cell
    const td = el.closest('td,th');
    if (td) {
      // preceding text inside the same cell (e.g. "$ [input]")
      let prevText = '';
      for (const n of td.childNodes) {
        if (n === el || n.contains && n.contains(el)) break;
        if (n.nodeType === 3) prevText += n.textContent; else if (n.nodeType === 1) prevText += n.innerText;
      }
      prevText = norm(prevText).replace(/[:\s]+$/, '');
      if (prevText && prevText.length <= 60 && !/^\$$/.test(prevText)) return prevText;
      let p = td.previousElementSibling;
      while (p && !norm(p.innerText)) p = p.previousElementSibling;
      if (p) return norm(p.innerText).replace(/[:\s]+$/, '');
    }
    // 4. preceding sibling text node
    let s = el.previousSibling;
    while (s && s.nodeType === 3 && !norm(s.textContent)) s = s.previousSibling;
    if (s && s.nodeType === 3) return norm(s.textContent).replace(/[:\s]+$/, '');
    return '';
  };
  const nameOf = (el, role) => {
    const t = el.tagName.toLowerCase();
    if (el.getAttribute('aria-label')) return norm(el.getAttribute('aria-label'));
    if (role === 'button') return norm(el.value || el.innerText || el.getAttribute('title') || el.alt);
    if (role === 'link' || role === 'heading' || role === 'cell') return norm(el.innerText);
    if (role === 'img') return norm(el.alt);
    if (role === 'textbox' || role === 'combobox' || role === 'checkbox' || role === 'radio')
      return labelFor(el) || norm(el.placeholder || el.getAttribute('title') || el.name);
    return norm(el.innerText);
  };
  const tableInfo = td => {
    const tr = td.parentElement; const table = td.closest('table');
    if (!tr || !table) return null;
    const rows = Array.from(table.rows);
    const rowIdx = rows.indexOf(tr); const colIdx = Array.from(tr.cells).indexOf(td);
    if (rowIdx < 0 || colIdx < 0) return null;
    const header = rows[0];
    const hdrCell = header && header.cells[colIdx];
    const isHeaderRow = rowIdx === 0 && rows.length > 1 && header.cells.length > 1;
    const rowKey = tr.cells[0];
    return {
      row_header: rowKey && rowKey !== td ? norm(rowKey.innerText) : (rowKey === td ? norm(td.innerText) : null),
      row_header_column: header && header.cells[0] && rowIdx > 0 ? norm(header.cells[0].innerText) : null,
      column_header: hdrCell && rowIdx > 0 ? norm(hdrCell.innerText) : (isHeaderRow ? norm(td.innerText) : null),
      is_header: isHeaderRow
    };
  };
  const prevCellLabel = td => {
    // legacy "Label: value" rows — the label is the previous non-empty cell in the same row
    let p = td.previousElementSibling;
    while (p && !norm(p.innerText)) p = p.previousElementSibling;
    if (!p) return '';
    const t = norm(p.innerText);
    return /[:：]$/.test(t) || p.querySelector('b,strong') || p.tagName === 'TH' ? t.replace(/[:：\s]+$/, '') : '';
  };
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    const role = roleOf(el);
    if (!role || !visible(el)) continue;
    if (role === 'cell') {
      // skip layout cells (contain other interactive/table elements or are empty)
      if (el.querySelector('table,input,select,button,a,textarea')) continue;
      if (!norm(el.innerText)) continue;
    }
    const r = el.getBoundingClientRect();
    const idx = refs.push(el) - 1;
    const item = {
      idx, role, name: nameOf(el, role), label: (role === 'textbox' || role === 'combobox' || role === 'checkbox' || role === 'radio') ? labelFor(el) : (role === 'cell' ? prevCellLabel(el) : ''),
      value: el.tagName.toLowerCase() === 'select' ? norm(el.options[el.selectedIndex]?.text) : (el.value !== undefined && role !== 'button' ? String(el.value) : ''),
      bbox: [r.left, r.top, r.width, r.height], disabled: !!el.disabled,
      input_type: (el.tagName.toLowerCase() === 'input' ? (el.type || 'text').toLowerCase() : ''),
      options: el.tagName.toLowerCase() === 'select' ? Array.from(el.options).map(o => norm(o.text)) : [],
      table: role === 'cell' ? tableInfo(el) : null
    };
    out.push(item);
  }
  return { elements: out, text: norm(document.body ? document.body.innerText : ''), title: document.title };
}
"""


def _frame_path(frame: Frame) -> list[str]:
    path: list[str] = []
    f: Optional[Frame] = frame
    while f is not None and f.parent_frame is not None:
        path.append(f.name or "?")
        f = f.parent_frame
    return list(reversed(path))


class PlaywrightSurface:
    """One live browser session. Shared by discovery, replay and the human operator."""

    def __init__(self, headless: bool = True, allowed_hosts: Optional[set[str]] = None,
                 slow_mo_ms: int = 0, viewport=(1100, 760)):
        self._pw: Playwright = sync_playwright().start()
        self._browser: Browser = self._pw.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        self._viewport = {"width": viewport[0], "height": viewport[1]}
        self._ctx = self._browser.new_context(viewport=self._viewport)
        self.page: Page = self._ctx.new_page()
        self._last_status: Optional[int] = None
        self._allowed_hosts = allowed_hosts
        self._ref_index: dict[str, tuple[Frame, int]] = {}
        self.page.on("response", self._on_response)
        # Native dialogs (alert/confirm) are a classic "unexpected dialog" runtime condition.
        # We record and dismiss them rather than let them hang the session.
        self.last_dialog: Optional[str] = None
        self.page.on("dialog", self._on_dialog)

    # ------------------------------------------------------------------ events
    def _on_response(self, resp):
        try:
            if resp.request.resource_type == "document":
                self._last_status = resp.status
        except PWError:
            pass

    def _on_dialog(self, dialog):
        self.last_dialog = f"{dialog.type}: {dialog.message}"
        dialog.dismiss()

    # --------------------------------------------------------------- observe
    def _settle(self, timeout_ms: int = 5000):
        try:
            self.page.wait_for_load_state("load", timeout=timeout_ms)
        except PWError:
            pass
        # frames load independently; give them a moment to attach
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            try:
                if all(f.url and f.url != "about:blank" for f in self.page.frames) or len(self.page.frames) == 1:
                    for f in self.page.frames:
                        f.wait_for_load_state("load", timeout=1000)
                    return
            except PWError:
                pass
            time.sleep(0.05)

    def observe(self) -> Observation:
        self._settle()
        self._ref_index.clear()
        elements: list[Element] = []
        texts: list[str] = []
        frame_locations: dict[str, str] = {}
        title = ""
        for fi, frame in enumerate(self.page.frames):
            try:
                snap = frame.evaluate(_SNAPSHOT_JS)
            except PWError as e:  # frame navigated mid-snapshot; retry once
                try:
                    time.sleep(0.1)
                    snap = frame.evaluate(_SNAPSHOT_JS)
                except PWError:
                    continue
            path = _frame_path(frame)
            if path:
                frame_locations["/".join(path)] = frame.url
            if frame.parent_frame is None:
                title = snap.get("title", "")
            for it in snap["elements"]:
                ref = f"f{fi}e{it['idx']}"
                self._ref_index[ref] = (frame, it["idx"])
                t = it.get("table")
                elements.append(Element(
                    ref=ref, role=it["role"], name=it["name"], label=it.get("label", ""),
                    value=it.get("value", ""), frame=path, bbox=tuple(it["bbox"]),
                    table=TableInfo(t["row_header"], t["row_header_column"], t["column_header"]) if t else None,
                    options=it.get("options", []), disabled=it.get("disabled", False),
                    input_type=it.get("input_type", ""),
                ))
            if snap.get("text"):
                texts.append(snap["text"])
        return Observation(elements=elements, visible_text="\n".join(texts), location=self.page.url,
                           http_status=self._last_status, title=title, frame_locations=frame_locations)

    # ------------------------------------------------------------------ act
    def _handle(self, ref: str):
        if ref not in self._ref_index:
            raise SurfaceError(f"stale or unknown element ref {ref!r}; observe() again")
        frame, idx = self._ref_index[ref]
        try:
            h = frame.evaluate_handle(f"() => window.__ah_refs[{idx}]").as_element()
        except PWError as e:
            raise SurfaceError(f"element {ref!r} no longer reachable: {e}") from e
        if h is None:
            raise SurfaceError(f"element {ref!r} detached")
        return h

    def click(self, ref: str) -> None:
        h = self._handle(ref)
        try:
            h.scroll_into_view_if_needed(timeout=3000)
            h.click(timeout=5000)
        except PWError as e:
            raise SurfaceError(f"click failed on {ref}: {e}") from e
        self._settle()

    def click_xy(self, frame: list[str], x: float, y: float) -> None:
        # Geometric fallback: coordinates are frame-relative; translate to page.
        target = self.page.main_frame
        for name in frame:
            nxt = next((f for f in target.child_frames if f.name == name), None)
            if nxt is None:
                raise SurfaceError(f"frame path {frame} not found")
            target = nxt
        off_x, off_y = 0.0, 0.0
        f = target
        while f.parent_frame is not None:
            fe = f.frame_element()
            box = fe.bounding_box() or {"x": 0, "y": 0}
            off_x += box["x"]
            off_y += box["y"]
            f = f.parent_frame
        self.page.mouse.click(off_x + x, off_y + y)
        self._settle()

    def type(self, ref: str, text: str, clear: bool = True) -> None:
        h = self._handle(ref)
        try:
            if clear:
                h.fill("")
            h.type(text, delay=10)
        except PWError as e:
            raise SurfaceError(f"type failed on {ref}: {e}") from e

    def select(self, ref: str, value: str) -> None:
        h = self._handle(ref)
        try:
            h.select_option(label=value)
        except PWError:
            try:
                h.select_option(value=value)
            except PWError as e:
                raise SurfaceError(f"select failed on {ref}: option {value!r} not found") from e

    def press(self, key: str) -> None:
        self.page.keyboard.press(key)
        self._settle()

    def navigate(self, target: str) -> None:
        try:
            self.page.goto(target, wait_until="load", timeout=15000)
        except PWError as e:
            raise SurfaceError(f"navigation to {target} failed: {e}") from e
        self._settle()

    def read_text(self, ref: str) -> str:
        h = self._handle(ref)
        try:
            tag = h.evaluate("e => e.tagName.toLowerCase()")
            if tag in ("input", "textarea"):
                return h.input_value()
            if tag == "select":
                return h.evaluate("e => e.options[e.selectedIndex]?.text || ''")
            return (h.inner_text() or "").strip()
        except PWError as e:
            raise SurfaceError(f"read failed on {ref}: {e}") from e

    def screenshot(self, path: str) -> None:
        try:
            self.page.screenshot(path=path, full_page=False)
        except PWError:
            pass

    def reset_session(self) -> None:
        """New browser context = new cookies = a fresh application session, same browser process."""
        self._ctx.close()
        self._ctx = self._browser.new_context(viewport=self._viewport)
        self.page = self._ctx.new_page()
        self._last_status, self.last_dialog = None, None
        self._ref_index.clear()
        self.page.on("response", self._on_response)
        self.page.on("dialog", self._on_dialog)

    def close(self) -> None:
        try:
            self._ctx.close()
            self._browser.close()
        finally:
            self._pw.stop()
