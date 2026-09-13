"""
Meridian Core — a deliberately *legacy* core-banking back-office console.

This is the stand-in for the real thing. It is built to be hostile to naive
automation, the way real bank back-office apps are:

  * framesets (top banner / left menu / main work area are separate frames)
  * server-rendered, table-based layouts, <font> tags, inline styles
  * NO ids, NO data-testid, NO stable classes — only what a human sees
  * one shared vendor product ("Meridian Core") configured for two tenants
    with different labels, branding and menu wording
  * runtime conditions that legitimately occur in production and can be
    injected on demand for tests: record-not-found, permission denied,
    validation error, a maintenance interstitial, session expiry,
    transient slowness and a hard 500.

Nothing here is real. Member numbers, names and balances are synthetic.
"""
from __future__ import annotations

import os
import time
import secrets
from dataclasses import dataclass, field

from flask import (Flask, Response, abort, redirect, render_template, request,
                   session, url_for)

# --------------------------------------------------------------------------
# Tenants — same vendor product, different configuration/branding/wording.
# --------------------------------------------------------------------------
TENANTS: dict[str, dict] = {
    "meridian": {
        "name": "Meridian Federal Credit Union",
        "product": "Meridian Core v7.2",
        "color": "#1f3a5f",
        "labels": {
            "member": "Member Number",
            "search_btn": "Search",
            "lookup_menu": "Member Lookup",
            "open_menu": "Open Sub-Account",
            "savings": "Regular Savings",
            "confirm_btn": "Confirm & Open",
            "nickname": "Account Nickname",
            "deposit": "Initial Deposit",
        },
    },
    "harbor": {
        "name": "Harbor Point Bank",
        "product": "Meridian Core v7.4 (Harbor build)",
        "color": "#4a1f5f",
        "labels": {
            "member": "Customer ID",
            "search_btn": "Find",
            "lookup_menu": "Customer Inquiry",
            "open_menu": "New Sub-Account",
            "savings": "Statement Savings",
            "confirm_btn": "Finalize",
            "nickname": "Account Label",
            "deposit": "Opening Deposit",
        },
    },
}

# Synthetic data. 99999 does not exist; 77777 is restricted; 50000 crashes.
MEMBERS: dict[str, dict] = {
    "12345": {"name": "Dana R. Whitfield", "status": "Active", "since": "2014-03-09",
              "accounts": [
                  {"type": "savings", "number": "S-0012345-01", "balance": "4,812.37"},
                  {"type": "checking", "number": "C-0012345-02", "balance": "1,204.10"}]},
    "23456": {"name": "Miguel A. Sandoval", "status": "Active", "since": "2019-11-21",
              "accounts": [
                  {"type": "savings", "number": "S-0023456-01", "balance": "912.00"}]},
    "34567": {"name": "Priya N. Raman", "status": "Dormant", "since": "2009-06-02",
              "accounts": [
                  {"type": "savings", "number": "S-0034567-01", "balance": "25.00"}]},
    "77777": {"name": "RESTRICTED", "status": "Restricted", "since": "—", "accounts": []},
}

DEMO_USER = os.environ.get("MERIDIAN_DEMO_USER", "operator1")
DEMO_PASS = os.environ.get("MERIDIAN_DEMO_PASS", "demo-pass-123")


@dataclass
class Chaos:
    """Per-session fault injection knobs, set via POST /__chaos (tests only)."""
    interstitial_once: bool = False      # show maintenance notice on next main-frame page
    slow_ms: int = 0                     # delay every main-frame response
    expire_session_in: int = -1          # after N main-frame requests, drop the session
    fail_next: bool = False              # next main-frame request returns HTTP 500


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.secret_key = os.environ.get("MERIDIAN_SECRET", secrets.token_hex(16))
    app.config["chaos"] = {}  # sid -> Chaos

    def chaos() -> Chaos:
        sid = session.get("sid")
        if not sid:
            sid = secrets.token_hex(8)
            session["sid"] = sid
        return app.config["chaos"].setdefault(sid, Chaos())

    def tenant(tid: str) -> dict:
        if tid not in TENANTS:
            abort(404)
        return TENANTS[tid]

    def logged_in() -> bool:
        return bool(session.get("user"))

    def apply_chaos_before_main() -> Response | None:
        c = chaos()
        if c.slow_ms:
            time.sleep(c.slow_ms / 1000.0)
        if c.fail_next:
            c.fail_next = False
            abort(500)
        if c.expire_session_in == 0:
            c.expire_session_in = -1
            session.pop("user", None)
        elif c.expire_session_in > 0:
            c.expire_session_in -= 1
        return None

    # ---------------- chaos control (not part of the "real" app) ----------
    @app.post("/__chaos")
    def set_chaos():
        c = chaos()
        for k, v in request.form.items():
            if k == "interstitial_once":
                c.interstitial_once = v == "1"
            elif k == "slow_ms":
                c.slow_ms = int(v)
            elif k == "expire_session_in":
                c.expire_session_in = int(v)
            elif k == "fail_next":
                c.fail_next = v == "1"
        return "ok"

    @app.get("/__health")
    def health():
        return "ok"

    # ---------------- frameset shell ---------------------------------------
    @app.get("/t/<tid>/")
    def shell(tid):
        t = tenant(tid)
        if not logged_in():
            return redirect(url_for("login", tid=tid))
        return render_template("shell.html", t=t, tid=tid)

    @app.get("/t/<tid>/banner")
    def banner(tid):
        return render_template("banner.html", t=tenant(tid), tid=tid,
                               user=session.get("user"))

    @app.get("/t/<tid>/menu")
    def menu(tid):
        return render_template("menu.html", t=tenant(tid), tid=tid)

    # ---------------- login -----------------------------------------------
    @app.route("/t/<tid>/login", methods=["GET", "POST"])
    def login(tid):
        t = tenant(tid)
        error = None
        if request.method == "POST":
            if request.form.get("uid") == DEMO_USER and request.form.get("pwd") == DEMO_PASS:
                session["user"] = request.form["uid"]
                return redirect(url_for("shell", tid=tid))
            error = "Invalid operator credentials."
        return render_template("login.html", t=t, tid=tid, error=error)

    @app.get("/t/<tid>/logout")
    def logout(tid):
        session.pop("user", None)
        return redirect(url_for("login", tid=tid))

    # ---------------- main work area ----------------------------------------
    def guard(tid):
        """Common pre-checks for main-frame pages. Returns a Response to short-circuit."""
        apply_chaos_before_main()
        if not logged_in():
            # Legacy apps typically bounce the *frame* to the login page.
            return render_template("session_expired.html", t=tenant(tid), tid=tid)
        c = chaos()
        if c.interstitial_once:
            c.interstitial_once = False
            session["interstitial_return"] = request.full_path
            return render_template("interstitial.html", t=tenant(tid), tid=tid)
        return None

    @app.post("/t/<tid>/interstitial/ack")
    def interstitial_ack(tid):
        back = session.pop("interstitial_return", None) or url_for("home", tid=tid)
        return redirect(back)

    @app.get("/t/<tid>/home")
    def home(tid):
        r = guard(tid)
        if r:
            return r
        return render_template("home.html", t=tenant(tid), tid=tid)

    @app.route("/t/<tid>/members", methods=["GET", "POST"])
    def member_lookup(tid):
        r = guard(tid)
        if r:
            return r
        t = tenant(tid)
        if request.method == "POST":
            mno = (request.form.get("mno") or "").strip()
            if not mno:
                return render_template("lookup.html", t=t, tid=tid,
                                       error=f"{t['labels']['member']} is required.")
            if mno == "50000":
                abort(500)
            if mno not in MEMBERS:
                return render_template("lookup.html", t=t, tid=tid,
                                       error=f"No record found for {t['labels']['member']} {mno}.",
                                       mno=mno)
            if MEMBERS[mno]["status"] == "Restricted":
                return render_template("lookup.html", t=t, tid=tid,
                                       error="Access denied: your role is not permitted to view this record.",
                                       mno=mno)
            return redirect(url_for("member_detail", tid=tid, mno=mno))
        return render_template("lookup.html", t=t, tid=tid)

    @app.get("/t/<tid>/members/<mno>")
    def member_detail(tid, mno):
        r = guard(tid)
        if r:
            return r
        m = MEMBERS.get(mno)
        if not m or m["status"] == "Restricted":
            abort(404)
        return render_template("detail.html", t=tenant(tid), tid=tid, mno=mno, m=m)

    @app.route("/t/<tid>/members/<mno>/subaccount/new", methods=["GET", "POST"])
    def subaccount_new(tid, mno):
        r = guard(tid)
        if r:
            return r
        t = tenant(tid)
        m = MEMBERS.get(mno)
        if not m:
            abort(404)
        if request.method == "POST":
            kind = request.form.get("kind", "")
            nick = (request.form.get("nick") or "").strip()
            dep = (request.form.get("dep") or "").strip()
            errors = []
            if not nick:
                errors.append(f"{t['labels']['nickname']} is required.")
            try:
                if float(dep) < 25:
                    errors.append(f"{t['labels']['deposit']} must be at least $25.00.")
            except ValueError:
                errors.append(f"{t['labels']['deposit']} must be a number.")
            if errors:
                return render_template("subaccount_form.html", t=t, tid=tid, mno=mno, m=m,
                                       errors=errors, kind=kind, nick=nick, dep=dep)
            session["pending_sub"] = {"kind": kind, "nick": nick, "dep": dep}
            return redirect(url_for("subaccount_review", tid=tid, mno=mno))
        return render_template("subaccount_form.html", t=t, tid=tid, mno=mno, m=m, errors=[])

    @app.route("/t/<tid>/members/<mno>/subaccount/review", methods=["GET", "POST"])
    def subaccount_review(tid, mno):
        r = guard(tid)
        if r:
            return r
        t = tenant(tid)
        m = MEMBERS.get(mno)
        p = session.get("pending_sub")
        if not m or not p:
            abort(404)
        if request.method == "POST":
            session.pop("pending_sub", None)
            new_no = f"{'S' if p['kind']=='savings' else 'X'}-{int(mno):07d}-{len(m['accounts'])+1:02d}"
            return render_template("subaccount_done.html", t=t, tid=tid, mno=mno, m=m,
                                   p=p, new_no=new_no)
        return render_template("subaccount_review.html", t=t, tid=tid, mno=mno, m=m, p=p)

    @app.get("/")
    def root():
        return redirect(url_for("shell", tid="meridian"))

    return app


def main():
    port = int(os.environ.get("MERIDIAN_PORT", "5055"))
    create_app().run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
