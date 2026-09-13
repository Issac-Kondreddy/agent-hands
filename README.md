# agent-hands

[![tests](https://github.com/Issac-Kondreddy/agent-hands/actions/workflows/tests.yml/badge.svg)](https://github.com/Issac-Kondreddy/agent-hands/actions/workflows/tests.yml)

**The layer that gives an AI agent hands inside legacy software with no API.**
An LLM works out how to complete a task inside a real UI *once*; the successful run is recorded as a typed, versioned, parameterised **capability**; that capability replays deterministically afterwards with **no model in the loop**, reports business outcomes vs. failures explicitly, and hands the live session to a human when it must.

> The model discovers. The artifact becomes a reusable capability. Deterministic replay is how the AI agent invokes it in production.

Design write-up: [`REPORT.md`](REPORT.md). Evidence of real runs: [`evidence/`](evidence/README.md).

## What is in the box

| Path | What |
|---|---|
| `target_app/` | **Meridian Core** — a deliberately hostile legacy bank back-office console: framesets, table layouts, no ids/test-ids, two tenants, injectable runtime faults. The stand-in for the real thing. |
| `agent_hands/schema.py` | The capability artifact schema (Pydantic): typed params/outputs, semantic anchors, expectations, condition rules, recovery blocks, tenant overlays. |
| `agent_hands/surface/` | The `Surface` seam and its Playwright implementation — an accessibility-style snapshot per frame, no CSS/XPath. |
| `agent_hands/locator.py` | The locator ladder: exact → label → table headers → fuzzy → near-text → geometric. Ambiguity is an error. |
| `agent_hands/policy.py` | Allowlist, risk classes, human-approval gate, redaction. The single choke point for every action. |
| `agent_hands/discovery/` | The LLM observe→decide→act loop (Claude tool-use) and the recorder that turns a run into an artifact. |
| `agent_hands/replay.py` | Deterministic replay engine with the `SUCCESS / BUSINESS_OUTCOME / FAILED / NEEDS_HUMAN / ABORTED` result contract. |
| `agent_hands/handoff.py` | Control lease state machine, intervention requests, live session, scripted & console operators. |
| `agent_hands/operator_web.py` | Minimal web operator console — a person takes over the same browser session, then hands it back. |
| `agent_hands/catalog.py` | Agent-facing capability catalog: function-calling descriptors, approval gate, `invoke()`. |
| `profiles/`, `overrides/` | App profile (product-level runtime conditions) and tenant overlays. |
| `artifacts/` | Saved capabilities (draft and approved versions). |
| `evidence/` | Logs, screenshots and snapshots from the real discovery run and replays. |
| `tests/` | 73 tests: schema, ladder, policy, redaction, lease, replay against the live app incl. every fault, handoff branches, hermetic discovery with a fake model, tenant overlay, catalog, web operator. |

## Setup

Requirements: Python 3.11+, Chromium via Playwright.

```bash
git clone <this repo> && cd agent-hands
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env        # fill in ANTHROPIC_API_KEY for discovery; not needed for replay or tests
set -a; source .env; set +a
```

Start the legacy target app in a second terminal (it stays up for everything below):

```bash
python -m target_app.app          # http://127.0.0.1:5055  (tenants: /t/meridian/ and /t/harbor/)
```

Synthetic demo operator: `operator1` / `demo-pass-123` (in `.env.example`). Synthetic members: `12345`, `23456`, `34567`; `99999` does not exist, `77777` is access-restricted, `50000` crashes the app.

### Running without live services

Replay and the entire test suite need **no API key** — the tests drive discovery with a scripted fake model (`tests/fake_llm.py`) to prove the recorder/replay plumbing, and the test app is started on a free port automatically:

```bash
python -m pytest            # ~2 min, launches headless Chromium
```

## Demo path

**1. Discover** — one real LLM-driven run against the live UI. The model gets the goal, a `${member_id}` placeholder with an example value, and the names of two secrets it can type but never see.

```bash
python -m agent_hands discover \
  --goal 'Look up member ${member_id} and read their current regular savings balance and full name' \
  --param member_id=12345 --param-desc '{"member_id":"5-digit member number"}'
# → artifacts/meridian_core.member_savings_balance.v1.json   (status: draft)
# → evidence/runs/discovery-<ts>/  run.jsonl, step-NN.png, step-NN.obs.txt
```

**2. Teach it a business outcome** (optional but recommended) — run the same goal on a member that does not exist; the model declares the condition and the artifact gets a new version with `MEMBER_NOT_FOUND` as a declared outcome:

```bash
python -m agent_hands discover --goal '...same goal...' --param member_id=99999 \
  --enrich artifacts/meridian_core.member_savings_balance.v1.json
# → artifacts/meridian_core.member_savings_balance.v2.json
```

**3. Review and approve** — only approved artifacts are invocable unattended:

```bash
python -m agent_hands approve --capability meridian_core.member_savings_balance --version 2 --reviewer you
python -m agent_hands catalog          # what an AI agent sees: name, params, returns, outcomes, risk
```

**4. Replay deterministically** — no model, different inputs, structured result:

```bash
python -m agent_hands replay --capability meridian_core.member_savings_balance --param member_id=23456
#  "status": "SUCCESS", "outputs": {"member_name": "Miguel A. Sandoval", "savings_balance": "912.00"}, "drift": []
python -m agent_hands replay --capability meridian_core.member_savings_balance --param member_id=99999
#  "status": "BUSINESS_OUTCOME", "outcome_code": "MEMBER_NOT_FOUND", "outcome_detail": "No record found for Member Number 99999"
```

**5. Replay under runtime conditions** — inject faults into the browser session before replay:

```bash
python -m agent_hands replay --capability ... --param member_id=12345 --chaos interstitial_once=1   # dismissed, continues
python -m agent_hands replay --capability ... --param member_id=12345 --chaos expire_session_in=3   # re-signs on, resumes
python -m agent_hands replay --capability ... --param member_id=12345 --chaos slow_ms=1500          # waits, no sleeps
python -m agent_hands replay --capability ... --param member_id=50000                                # FAILED / APP_ERROR
```

**6. Same artifact, second tenant** — Harbor Point Bank runs the same product with different captions; `overrides/harbor.json` remaps anchors:

```bash
python -m agent_hands replay --capability meridian_core.member_savings_balance --param member_id=23456 --tenant harbor
```

**7. Human handoff** — the sub-account flow has an irreversible "Confirm & Open" step. Without an operator the run stops as `NEEDS_HUMAN`; with `--operator web` a local console at http://127.0.0.1:5077 lets you act on the live browser and hand control back:

```bash
python -m agent_hands replay --capability meridian_core.open_savings_subaccount --allow-draft \
  --param member_id=12345 --param nickname=Vacation --param deposit=100 --operator web --headed
```

`--operator console` does the same at the terminal. `--operator auto-approve` is a **dev-only** scripted approver used to generate unattended evidence; it is documented as a mock and must never be used against a real system.

**8. Stability signal**:

```bash
python -m agent_hands stability --capability meridian_core.member_savings_balance --param member_id=12345 --runs 5
```

## Reading a result

```json
{ "status": "FAILED", "outcome_code": "EXPECTATION_FAILED", "failed_step": "s06_search_for_member",
  "expected": "text visible: 'Member Detail'", "observed": "Member Lookup ... No record found for Member Number 99999.",
  "evidence_dir": "evidence/runs/replay-...", "steps": [{"step_id": "...", "rung": "exact", "confidence": 1.0, ...}] }
```

`status` is the contract: `SUCCESS` (outputs filled, checkpoint verified), `BUSINESS_OUTCOME` (a declared, legitimate non-success answer with `outcome_code`), `FAILED` (hard failure with step / expected / observed and a screenshot + snapshot in `evidence_dir`), `NEEDS_HUMAN` (stopped, no operator attached), `ABORTED` (an operator took over and stopped it). `drift` lists steps that resolved below the stable rungs of the locator ladder.

## Configuration

* `ANTHROPIC_API_KEY` — discovery only. `AGENT_HANDS_MODEL` overrides the model (default `claude-sonnet-5`; if that id is unknown to the API the agent falls back to the newest Sonnet it can list).
* `MERIDIAN_DEMO_USER` / `MERIDIAN_DEMO_PASS` — the synthetic operator credentials, typed as `${secret:...}`; never written to artifacts or logs.
* `--policy policy.json` — allowlist/risk/redaction as JSON (see `Policy` in `agent_hands/policy.py`); the default is the local target's allowlist with `/logout` and `/__chaos` denied and irreversible actions requiring a human.

## Notes

* Secrets stay out of the repo: `.env` is git-ignored; evidence is redacted before it is written; artifacts hold placeholders only.
* The target app, its members and balances are synthetic. Nothing here touches a real bank system.
* AI-assisted development was used throughout, as the brief assumes; every design decision is explained in `REPORT.md` and defended in code comments.
