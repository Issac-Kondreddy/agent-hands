# Evidence

Every run — discovery or replay — writes one directory under `runs/`:

```
runs/<kind>-<timestamp>-<id>/
  run.jsonl        one JSON event per line: what happened, why, when, and WHO HELD CONTROL (`controller`)
  step-NN.png      screenshot after each action (discovery) / at failure, outcome, intervention and final (replay)
  step-NN.obs.txt  the accessibility snapshot the model or engine saw at that moment — the "DOM snapshot" analogue
  result.json      the structured result returned to the caller
```

Everything here went through the redactor before it hit disk: secrets typed by name are never present, account numbers are `[REDACTED account_no]` (the *caller* still receives the real value — compare `outputs` in `result.json` with the replay's stdout).

Model: `claude-sonnet-5` via the Anthropic API. Target: the local Meridian Core legacy console in `target_app/`. All data synthetic.

## Discovery runs (model in the loop)

| Run | Goal | Inputs | Result | Model calls | What it shows |
|---|---|---|---|---|---|
| `discovery-20260913T005607-35ee85` | look up member, read savings balance + name | member_id=12345 | **SUCCESS** → `member_savings_balance.v1` | 7 | The genuine LLM-driven run. Model types `${secret:...}` for credentials and `${member_id}` for the input, reads the two cells, calls `done` with literal screen text. `step-00…08.png` are the screens it saw. |
| `discovery-20260913T005623-4d417c` | same goal | member_id=99999 | **ENRICHED** → `.v2` | 7 | Model meets "No record found", calls `declare_condition(business_outcome, MEMBER_NOT_FOUND)`, reports the outcome. Artifact gains a rule + declared outcome; flow steps untouched. |
| `discovery-20260913T005735-7e1b62` | same goal | member_id=77777 | **ENRICHED** → `.v3` | 7 | Same for "Access denied" → `ACCESS_DENIED`. |
| `discovery-20260913T005942-1dc61f` | open a savings sub-account, read new account number | 12345 / Vacation / 100 | **SUCCESS** → `open_regular_savings_subaccount.v1` | 11 | Multi-field form → review → **irreversible commit**. At seq 21 the policy refuses "Confirm & Open" (`policy.gate HUMAN_APPROVAL_REQUIRED`), an intervention is raised, the control lease transfers to the operator, the operator approves, control returns, the model continues. The operator here is the documented `auto-approve` scripted stand-in. |
| `discovery-20260913T010006-bdb062` | same goal | deposit=5 | **ENRICHED** → `.v2` | 10 | Model meets "Initial Deposit must be at least $25.00", declares `DEPOSIT_BELOW_MINIMUM`. |

## Replay runs (no model)

Capability `meridian_core.member_savings_balance` (v3 unless noted):

| Run | Inputs / injected condition | Result | What it shows |
|---|---|---|---|
| `replay-20260913T005808-fa753c` | member_id=23456, **tenant=harbor** | **SUCCESS** name=Miguel A. Sandoval, balance=912.00, drift=[] | Artifact recorded on Meridian FCU replays on Harbor Point Bank ("Customer ID", "Find", "Statement Savings") through `overrides/harbor.json`. Different member than the recording. |
| `replay-20260913T005810-506c2a` | member_id=99999, tenant=harbor | **BUSINESS_OUTCOME MEMBER_NOT_FOUND** "No record found for Customer ID 99999" | Outcome detection survives the tenant's wording. |
| `replay-20260913T005751-e2ed71` | member_id=77777 | **BUSINESS_OUTCOME ACCESS_DENIED** | A permission denial is an answer, not a crash. |
| `replay-20260913T005650-9de00a` | member_id=77777 on **v2** (before it learned ACCESS_DENIED) | **FAILED EXPECTATION_FAILED** step `s06_search_for_member`, expected "Member Detail", observed "…Access denied…" | What an *unknown* condition looks like: a hard failure with expected/observed and `failure-s06….png`. The next enrich run turned this into a declared outcome. |
| `replay-20260913T005754-efe510` | `interstitial_once=1` | **SUCCESS** | "System Notice" interstitial detected by the app-profile rule, `Acknowledge` clicked (`condition.dismiss`), step re-run, flow continues. |
| `replay-20260913T005756-38f2e4` | `expire_session_in=3` | **SUCCESS** | Session expires mid-flow; `re_signon` recovery block runs; flow restarts from `s05_enter_member_number` (`resume_from_step`), outputs correct. |
| `replay-20260913T005759-735d33` | `slow_ms=1500` | **SUCCESS** | Every step waited on its declared expectation; no sleeps, no timeouts. |
| `replay-20260913T005716-5f540d` | member_id=50000 (app returns HTTP 500) | **FAILED APP_ERROR** "application returned HTTP 500" | Built-in hard failure with the failing step and a screenshot. |

Capability `meridian_core.open_regular_savings_subaccount` (v2):

| Run | Inputs | Result | What it shows |
|---|---|---|---|
| `replay-20260913T010049-5fc910` | 12345 / Vacation / 100, no operator | **NEEDS_HUMAN HUMAN_APPROVAL_REQUIRED** at `s11_confirm_and_open_the_sub` | Unattended replay stops before the irreversible step; `intervention-s11….png` + snapshot carry the context a person needs. |
| `replay-20260913T010052-9d6903` | **23456** / Vacation / 100, operator approves | **SUCCESS** new_account_number=S-0023456-02 | Handoff round-trip in `run.jsonl` (`control.transfer` ×4, `intervention.decision`); a *different member* than the recording, proving the flow generalised. In `result.json` the account number is redacted; the caller got the real value. |
| `replay-20260913T010055-dad9e4` | deposit=5 | **BUSINESS_OUTCOME DEPOSIT_BELOW_MINIMUM** | Validation error returned as a declared outcome, with the message captured. |

Stability (`python -m agent_hands stability … --runs 5`, not kept as directories): 5/5 SUCCESS, 1 distinct output, 0 drift runs, mean 1.35 s.

## Reading `run.jsonl`

Event types you will see: `run.start`, `navigate`, `step.start`, `step.resolved` (anchor, **rung**, confidence, the element it matched), `step.type` / `step.read`, `step.ok`, `step.skipped` (a `skip_if` precondition held), `condition.matched` / `condition.dismiss` / `condition.retry` / `condition.recovery`, `expectation.failed` (expected, observed, screenshot), `policy.gate` / `policy.violation`, `intervention.requested`, `control.transfer`, `human.action`, `intervention.decision`, `checkpoint.ok`, `run.end`. Discovery runs add `llm.response` (the model's text and tool calls, token usage), `agent.action`, `agent.condition`, `agent.done_rejected`.
