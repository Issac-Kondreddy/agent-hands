"""
The Surface protocol — the seam between "how we perceive/act on a UI" and
"the recorded flow".

Everything above this line (discovery agent, recorder, replay engine,
handoff) speaks only in terms of `Observation`s and `Element`s. Everything
below it (Playwright today; a Windows UIA / macOS AX / screenshot+OCR surface
tomorrow) has to answer just seven questions:

    what is on screen?  (observe)      →  roles, names, labels, geometry
    click this          (click)
    type into this      (type)
    choose this option  (select)
    press a key         (press)
    go here             (navigate)     — may be a URL, a window, or a menu path
    take a picture      (screenshot)

Deliberately *not* in the protocol: CSS selectors, XPath, DOM handles. If a
concept only exists in a browser it does not belong here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


@dataclass
class TableInfo:
    row_header: Optional[str] = None
    row_header_column: Optional[str] = None
    column_header: Optional[str] = None


@dataclass
class Element:
    ref: str                          # ephemeral handle valid until the next observe()
    role: str                         # button | textbox | link | combobox | cell | heading | text | ...
    name: str = ""                    # accessible name
    label: str = ""                   # nearby/associated visible label (legacy table-form style)
    value: str = ""                   # current value for inputs / selected option text
    frame: list[str] = field(default_factory=list)
    bbox: tuple[float, float, float, float] = (0, 0, 0, 0)
    table: Optional[TableInfo] = None
    options: list[str] = field(default_factory=list)   # for combobox
    disabled: bool = False
    input_type: str = ""              # 'password' etc. — used to suppress logging of typed values

    def describe(self) -> str:
        parts = [f"[{self.ref}] {self.role}"]
        if self.name:
            parts.append(f'"{self.name}"')
        if self.label and self.label != self.name:
            parts.append(f"(label: {self.label})")
        if self.value and self.input_type != "password":
            parts.append(f"value={self.value!r}")
        if self.table and self.table.column_header:
            parts.append(f"[row: {self.table.row_header!r}, col: {self.table.column_header!r}]")
        if self.options:
            parts.append(f"options={self.options}")
        if self.frame:
            parts.append(f"frame={'/'.join(self.frame)}")
        if self.disabled:
            parts.append("DISABLED")
        return " ".join(parts)


@dataclass
class Observation:
    elements: list[Element]
    visible_text: str                 # concatenated visible text across frames (for condition matching)
    location: str                     # URL for web; window title/path for desktop
    http_status: Optional[int] = None # last main-document status, when the surface knows it
    title: str = ""
    frame_locations: dict[str, str] = field(default_factory=dict)  # frame path -> URL (framesets!)

    def all_locations(self) -> list[str]:
        return [self.location, *self.frame_locations.values()]

    def find(self, ref: str) -> Optional[Element]:
        for e in self.elements:
            if e.ref == ref:
                return e
        return None

    def render(self, max_elements: int = 120) -> str:
        """Compact text rendering for the LLM. Interactive controls first, then cells/text."""
        interactive = [e for e in self.elements if e.role in {"button", "link", "textbox", "combobox", "checkbox", "radio"}]
        passive = [e for e in self.elements if e not in interactive]
        lines = [f"LOCATION: {self.location}"]
        lines += [f"FRAME {k}: {v}" for k, v in self.frame_locations.items()]
        lines += [f"TITLE: {self.title}", "INTERACTIVE CONTROLS:"]
        lines += ["  " + e.describe() for e in interactive[:max_elements]]
        lines.append("READABLE CELLS / TEXT:")
        lines += ["  " + e.describe() for e in passive[:max_elements]]
        return "\n".join(lines)


class SurfaceError(RuntimeError):
    """The surface could not perform the action (stale ref, navigation failed, app crashed)."""


@runtime_checkable
class Surface(Protocol):
    def observe(self) -> Observation: ...
    def click(self, ref: str) -> None: ...
    def click_xy(self, frame: list[str], x: float, y: float) -> None: ...
    def type(self, ref: str, text: str, clear: bool = True) -> None: ...
    def select(self, ref: str, value: str) -> None: ...
    def press(self, key: str) -> None: ...
    def navigate(self, target: str) -> None: ...
    def read_text(self, ref: str) -> str: ...
    def screenshot(self, path: str) -> None: ...
    def close(self) -> None: ...
