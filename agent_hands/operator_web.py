"""
WebOperator — a minimal, real operator surface.

When automation escalates, a tiny local HTTP page shows the intervention
request (why we stopped, which step, screenshot, the live accessibility
snapshot) and lets a person act on the *same* browser session by ref, then
hand control back with one of the four decisions.

It is intentionally plain (no JS framework, no websockets): the brief says
the co-browsing console is out of scope; what matters is that the handoff
seam is real — every human action flows through `LiveSession`, is
lease-checked and lands in the same evidence log as the automation's.
"""
from __future__ import annotations

import html
import queue
import threading
from pathlib import Path
from typing import Optional

from flask import Flask, Response, redirect, request, send_file
from werkzeug.serving import make_server

from .handoff import Decision, InterventionRequest, LiveSession


class WebOperator:
    human_id = "web-operator"

    def __init__(self, port: int = 5077, human_id: str = "web-operator", open_browser: bool = False):
        self.port, self.human_id, self._open = port, human_id, open_browser
        self._app = Flask("agent-hands-operator")
        self._server = None
        self._req: Optional[InterventionRequest] = None
        self._session: Optional[LiveSession] = None
        self._decision: Optional[Decision] = None
        self._done = threading.Event()
        self._evidence_root: Optional[Path] = None
        # Playwright's sync API is single-threaded: HTTP handlers enqueue work, the thread that owns the
        # browser (the one blocked in handle()) executes it. This is also what keeps "one lease holder" honest.
        self._cmds: "queue.Queue[tuple]" = queue.Queue()
        self._routes()

    def _call(self, fn, *args):
        """Run fn(*args) on the browser-owning thread and return its result (or raise)."""
        ev, box = threading.Event(), {}
        self._cmds.put((fn, args, ev, box))
        ev.wait()
        if "error" in box:
            raise box["error"]
        return box.get("result")

    # ------------------------------------------------------------ operator protocol
    def handle(self, request_: InterventionRequest, session: LiveSession) -> Decision:
        self._req, self._session, self._decision = request_, session, None
        self._done.clear()
        self._evidence_root = Path(session.evidence_root)
        self._ensure_server()
        url = f"http://127.0.0.1:{self.port}/"
        print(f"\n*** INTERVENTION REQUESTED — open {url} to take control of the live session ***\n", flush=True)
        if self._open:
            import webbrowser
            webbrowser.open(url)
        while not self._done.is_set():
            try:
                fn, args, ev, box = self._cmds.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                box["result"] = fn(*args)
            except Exception as ex:  # noqa: BLE001
                box["error"] = ex
            finally:
                ev.set()
        return self._decision or Decision("abort", "operator page closed", self.human_id)

    def _ensure_server(self):
        if self._server is None:
            self._server = make_server("127.0.0.1", self.port, self._app, threaded=True)
            threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def shutdown(self):
        if self._server:
            self._server.shutdown()
            self._server = None

    # ------------------------------------------------------------ routes
    def _routes(self):
        app = self._app

        @app.get("/")
        def index():
            r = self._req
            if r is None:
                return "<p>No intervention pending.</p>"
            obs = self._call(self._session.observe)
            e = html.escape
            rows = "".join(f"<tr><td><code>{e(el.ref)}</code></td><td>{e(el.role)}</td><td>{e(el.name)}</td>"
                           f"<td>{e(el.label)}</td><td>{e('/'.join(el.frame))}</td></tr>" for el in obs.elements)
            return f"""<html><head><title>agent-hands operator</title>
<style>body{{font-family:system-ui;margin:20px;max-width:1100px}} table{{border-collapse:collapse;font-size:12px}}
td,th{{border:1px solid #ccc;padding:3px 6px}} .box{{border:1px solid #c89000;background:#fffbe0;padding:10px;margin:10px 0}}
form.inline{{display:inline}} img{{max-width:100%;border:1px solid #999}}</style></head><body>
<h2>Intervention requested — you hold the live session</h2>
<div class="box"><b>Capability:</b> {e(r.capability_id)} &nbsp; <b>Step:</b> {e(str(r.step_id))} — {e(r.step_description)}<br>
<b>Why automation stopped:</b> [{e(r.reason_code)}] {e(r.reason)}<br><b>Location:</b> {e(obs.location)}</div>
<h3>Decide</h3>
<form class="inline" method="post" action="/decide"><input type="hidden" name="kind" value="approve_step">
<input name="note" placeholder="note"><button>Approve this step (automation executes it)</button></form>
<form class="inline" method="post" action="/decide"><input type="hidden" name="kind" value="resume"><button>I did it manually — resume</button></form>
<form class="inline" method="post" action="/decide"><input type="hidden" name="kind" value="skip_step"><button>Skip this step</button></form>
<form class="inline" method="post" action="/decide"><input type="hidden" name="kind" value="abort"><input name="note" placeholder="reason"><button>Abort run</button></form>
<h3>Act on the live session</h3>
<form method="post" action="/act">action <select name="action"><option>click</option><option>type</option><option>select</option><option>press</option></select>
ref <input name="ref" size="8"> value/key <input name="value" size="30"> <button>Do it</button></form>
<h3>Screenshot (live)</h3><img src="/shot?t={int(r.created_at*1000)}">
<h3>Controls on screen</h3><table><tr><th>ref</th><th>role</th><th>name</th><th>label</th><th>frame</th></tr>{rows}</table>
</body></html>"""

        @app.get("/shot")
        def shot():
            path = self._call(self._session.screenshot, "operator-view")
            root = self._evidence_root or Path(".")
            full = root / path if not Path(path).is_absolute() else Path(path)
            return send_file(str(full), mimetype="image/png") if full.exists() else Response(status=404)

        @app.post("/act")
        def act():
            a, ref, val = request.form["action"], request.form.get("ref", ""), request.form.get("value", "")
            try:
                if a == "click":
                    self._call(self._session.click, ref)
                elif a == "type":
                    self._call(self._session.type, ref, val)
                elif a == "select":
                    self._call(self._session.select, ref, val)
                elif a == "press":
                    self._call(self._session.press, val)
            except Exception as ex:  # noqa: BLE001
                return f"<p>error: {html.escape(str(ex))}</p><a href='/'>back</a>"
            return redirect("/")

        @app.post("/decide")
        def decide():
            self._decision = Decision(request.form["kind"], request.form.get("note", ""), self.human_id)  # type: ignore[arg-type]
            self._done.set()
            return "<p>Control handed back to automation. You can close this tab.</p>"
