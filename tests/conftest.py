"""Shared fixtures: a live legacy target app on a free port, a browser surface, a policy."""
from __future__ import annotations

import os
import socket
import threading
import time
import urllib.request

import pytest
from werkzeug.serving import make_server

from agent_hands.policy import Policy
from agent_hands.surface.playwright_surface import PlaywrightSurface
from target_app.app import create_app

os.environ.setdefault("MERIDIAN_DEMO_USER", "operator1")
os.environ.setdefault("MERIDIAN_DEMO_PASS", "demo-pass-123")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def app_url():
    port = _free_port()
    app = create_app()
    srv = make_server("127.0.0.1", port, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/__health", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    yield base
    srv.shutdown()


@pytest.fixture(scope="session")
def policy(app_url):
    return Policy.for_local_target(app_url.split("//")[1])


@pytest.fixture
def surface():
    s = PlaywrightSurface(headless=True)
    yield s
    s.close()


@pytest.fixture
def chaos(surface, app_url):
    """Inject a runtime condition into the *browser's* session (cookie-scoped)."""
    def _set(**flags):
        # Navigate once so the browser has a session cookie, then POST via fetch in-page.
        if surface.page.url == "about:blank":
            surface.page.goto(app_url + "/__health")
        body = "&".join(f"{k}={v}" for k, v in flags.items())
        surface.page.evaluate(
            "b => fetch('/__chaos', {method:'POST', body:b, headers:{'Content-Type':'application/x-www-form-urlencoded'}}).then(r=>r.text())",
            body)
    return _set
