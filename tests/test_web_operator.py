"""The web operator console: a person (simulated by HTTP calls) takes over the live session and hands it back."""
import json
import socket
import threading
import time
import urllib.parse
import urllib.request

import pytest

from agent_hands.operator_web import WebOperator
from agent_hands.replay import ReplayEngine
from tests.fixtures import open_subaccount_capability

pytestmark = pytest.mark.timeout(120)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post(url, **form):
    data = urllib.parse.urlencode(form).encode()
    return urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10).read().decode()


def _get(url):
    return urllib.request.urlopen(url, timeout=10).read().decode()


def test_person_clicks_confirm_in_console_then_resumes(surface, policy, app_url, tmp_path):
    port = _free_port()
    op = WebOperator(port=port)
    base = f"http://127.0.0.1:{port}"
    seen = {}

    def person():
        # wait for the console to come up with a pending request, read it, act, resume
        for _ in range(100):
            try:
                page = _get(base + "/")
                if "Intervention requested" in page:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        seen["page"] = page
        ref = page.split('<td><code>')[1:]  # crude: find the ref of the Confirm button row
        confirm_ref = next(r.split("</code>")[0] for r in ref if "Confirm &amp; Open" in r)
        _post(base + "/act", action="click", ref=confirm_ref, value="")
        seen["after"] = _get(base + "/")
        _post(base + "/decide", kind="resume", note="clicked confirm from the console")

    t = threading.Thread(target=person, daemon=True)
    t.start()
    cap = open_subaccount_capability(app_url + "/t/meridian/")
    try:
        r = ReplayEngine(surface, policy, evidence_dir=str(tmp_path), operator=op).run(
            cap, {"member_id": "12345", "nickname": "Console", "deposit": "50"})
    finally:
        op.shutdown()
    t.join(5)
    assert r.status == "SUCCESS" and r.interventions == 1
    assert "HUMAN_APPROVAL_REQUIRED" in seen["page"] and "you hold the live session" in seen["page"]
    events = [json.loads(l) for l in open(f"{r.evidence_dir}/run.jsonl")]
    human = [e for e in events if e["type"] == "human.action"]
    assert len(human) == 1 and human[0]["controller"] == "human:web-operator" and "Confirm" in human[0]["target"]
    decision = next(e for e in events if e["type"] == "intervention.decision")
    assert decision["kind"] == "resume" and "console" in decision["note"]
