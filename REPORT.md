# Design write-up — agent-hands

## 1. Architecture

The system is a single Python process with four layers and one seam between each.

```
  goal + params ──▶ DiscoveryAgent (LLM, once) ──▶ Recorder ──▶ Capability artifact (JSON, versioned)
                          │                                            │
                          │ observe/act                                │ approve
                          ▼                                            ▼
                     Surface  ◀──────────── ReplayEngine (no LLM) ◀── Catalog.invoke(name, params)
                  (Playwright a11y)               │
                          ▲                       │ stuck / irreversible / unrecoverable
                          │ same session          ▼
                     LiveSession  ◀────── ControlLease ◀────── Operator (web | console | scripted)
```

**Surface** (`surface/base.py`) is the boundary between "how we perceive and act on a UI" and everything else. It answers seven questions — observe, click, type, select, press, navigate, screenshot — and speaks only in roles, names, labels, table headers, frame paths and geometry. It has no notion of CSS, XPath or DOM handles. The Playwright implementation computes its own accessibility-style snapshot per frame (framesets included), including the legacy convention "label is the previous table cell". Everything above it is surface-agnostic by construction, which is what makes the desktop story in §4 credible rather than aspirational.

**Policy** (`policy.py`) is the single choke point. Both discovery and replay call `Policy.check()` before every action; there is no code path that reaches the surface without it. It owns the host/route allowlist, the action allowlist, the risk classification and the redactor.

**Discovery** is the only place a model appears. It runs an observe → decide → act loop with Claude tool-use. The model refers to controls by ephemeral refs; the *recorder* — not the model — decides how a control is anchored. The model cannot pick a brittle selector because it never picks a selector at all. Parameters are typed as `${member_id}` placeholders and secrets as `${secret:NAME}`; the model never sees a credential value, and the recorded step is parameterised before it is written.

**Replay** loads an artifact, applies a tenant overlay if requested, and executes the flow with no model: observe → evaluate condition rules → resolve the target through the locator ladder → policy check → act → verify the step's expectation by polling → record which rung resolved. The result contract is one of five statuses, and `BUSINESS_OUTCOME` is deliberately not `FAILED`.

**Handoff** is a lease. `ControlLease` is a five-state machine; only the lease holder may act; every evidence event is stamped with the holder. The operator surface is pluggable behind a two-method protocol so the console can be as thin as the brief allows while the control-transfer model stays real.

Key trade-offs: Python + Playwright (mature, readable, and Playwright's frame model handles framesets cleanly) over a CUA SDK (would hide exactly the locator and policy decisions being evaluated). A single synchronous process over services or queues — the brief asks for judgment, not plumbing, and the seams (`Surface`, `Operator`, `Catalog.invoke`) are where a queue or an RPC boundary would go. Our own perception layer over `page.accessibility` — because the interesting legacy cases (label-in-adjacent-cell, header-addressed table cells) are not what a browser's native tree gives you, and because a desktop surface will have to produce the same `Element` shape anyway.

Target: a local, deliberately hostile legacy core-banking console ("Meridian Core") rather than a public demo site. It has framesets, table layouts, `<font>` tags, zero ids or test-ids, a second tenant with different captions, and every runtime condition in the brief injectable on demand. A public e-commerce sandbox would have exercised none of the things Section 1 says matter.

## 2. Artifact schema

A capability is a *function*, not a macro. The top level carries the signature an agent reads (`parameters`, `outputs`, `outcomes`, `max_risk`, `requires_human_approval`) and a `status` (`draft → approved → deprecated`); only approved artifacts are invocable unattended. Version and a content `fingerprint` make every change a reviewable diff.

`Anchor` describes a control the way an operator would: role + accessible name, role + label, table cell by *header text* (never row/column index), distinctive nearby text, frame *name* path, and a recorded bounding box that is used only for disambiguation unless `allow_geometric_fallback` is explicitly set. Each anchor has a `rationale` the recorder writes for reviewers. The recorder refuses to anchor a value cell by its own text: a cell read for "$4,812.37" is anchored by *Regular Savings × Current Balance*, and the recorder also strips any header that happens to contain this run's input value.

`Step` carries an `expect` post-condition (text, pattern, control presence, URL) with a bounded timeout, and a `skip_if` precondition that makes steps idempotent — sign-on is skipped if the session is already signed on. Reaching the last step is not success; the `checkpoint` is.

`ConditionRule` is the piece the brief calls the most common design mistake. Each rule says *when the surface looks like X, it means Y, so do Z*, and Y is one of three kinds with a constrained response set: business outcomes may only `return_outcome`; recoverables may only `dismiss`, `retry` or `run_recovery` (with `resume_from_step`, because recovery resets UI state); hard failures may only `fail` or `escalate`. Every recoverable response is bounded by `max_attempts` per run, so replay always terminates. Product-level rules (session expiry, maintenance notice, auth failure) live in an *app profile* shared by every capability on that product; flow-specific outcomes (not found, validation) are declared by the model during discovery, and a later "enrich" run can teach an existing artifact a new outcome as a new version without touching its steps.

Values are never stored — only `${param}` and `${secret:NAME}` references. `sensitive` parameters and outputs are redacted from all evidence.

## 3. Determinism & error handling

Replay is deterministic because every source of variation is removed or bounded: fixed step order; anchors resolved by a fixed ladder (exact → label → table → fuzzy → near → geometric) where ambiguity is an error, not a coin toss; bounded fixed-interval polling against *declared* expectations instead of sleeps; run-level bounds on every rule; no randomness and no model. Which rung resolved each step is recorded; anything below the top three is reported as `drift`, and the `stability` command replays N times and reports success rate, distinct outputs and drift count.

Runtime conditions are evaluated at three moments: before each step (conditions left by the previous one), while a target cannot be found (the page is usually telling you why), and while an expectation is unmet. Order is business outcome → recoverable → hard failure, so "No record found" is never mistaken for a broken locator. Built-ins that every capability gets for free: HTTP 5xx on the main document is `APP_ERROR`; native dialogs are dismissed and logged. The demonstrated matrix on the target app: not found and access denied → `BUSINESS_OUTCOME`; validation error → `BUSINESS_OUTCOME` with the message captured; maintenance interstitial → dismissed and continued; session expiry → recovery block re-signs on and resumes from the first post-sign-on step; slow page → waited for; HTTP 500 → `FAILED APP_ERROR`; a missing control → `NEEDS_HUMAN` or, with an operator, a handoff. On any failure the result names the step, what was expected and what was observed, and the evidence directory holds a screenshot plus the accessibility snapshot at that moment.

UI drift is secondary in this environment, and is handled by the ladder plus the drift signal rather than by self-healing: a replay that slid to the fuzzy rung still succeeds, but the artifact is flagged for re-review.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is the `Surface` protocol and the `Element` shape. A legacy web app is already covered — the target *is* one. A desktop app needs a surface that produces the same elements from the platform accessibility API (UIA on Windows, AX on macOS): roles, names, labels-by-proximity and bounding boxes all exist there; "frame path" becomes "window / pane path"; `navigate` becomes "open app / menu path". The replay engine, ladder, rules, lease and evidence do not change. A screenshot-only surface (OCR + geometry) would populate `name` from OCR and lean on the `near` and `geometric` rungs, which is why those rungs exist at all. The artifact records `surface: legacy_web | desktop`; the geometric rung's coordinates are frame/window-relative for the same reason.

**Multi-tenant reuse.** Anchors have stable ids, and a `TenantOverlay` remaps names, labels, table headers, entry point and literal text for a tenant running the same vendor product — an overlay, not a fork. The Harbor Point tenant relabels "Member Number" to "Customer ID", "Search" to "Find", "Member Lookup" to "Customer Inquiry" and "Regular Savings" to "Statement Savings"; the artifact recorded on Meridian replays on Harbor with zero drift, and the not-found outcome comes back with Harbor's wording. If a tenant's *flow* differs, that is a fork and the loader refuses to pretend otherwise. Drift detection per tenant is the rung/drift signal on every replay plus the stability command; version drift would be managed by pinning an overlay to a base artifact version and treating a drift streak as a trigger for re-review or an enrich run.

## 5. Escalation & handoff

**Detect.** Automation asks for a person on four codes: `HUMAN_APPROVAL_REQUIRED` (an irreversible control — policy raises risk by control caption, and an artifact can also mark a step `requires_human_approval`), `TARGET_NOT_FOUND` / `AMBIGUOUS_TARGET` after the bounded wait, `UNRECOVERABLE` (a rule whose response is `escalate`), and during discovery `STUCK` (the model says so) — the model is also refused any irreversible action it has not been approved for.

**Route.** An `InterventionRequest` carries capability, goal, step, reason code, reason, location, screenshot and the live accessibility snapshot, and is written to the evidence log.

**Transfer.** `ControlLease` moves `AUTOMATION → INTERVENTION_REQUESTED → HUMAN → RESUMING → AUTOMATION` (or `ABORTED`). The human gets a `LiveSession`: the same Playwright page, lease-checked on every call, logging every action with `controller=human:<id>`. Because Playwright is single-threaded, the web console marshals each action onto the thread that owns the browser — which is also what keeps "one holder" literally true.

**Hand back.** Four decisions: `approve_step` (automation executes the gated step with approval), `resume` (the person did some or all of it; the engine checks whether the step's expectation already holds and skips or re-runs), `skip_step`, `abort`. During discovery a human's actions are recorded into the artifact as steps, parameterised like any other, and flagged `(human)` for review.

What is mocked: the operator console is a plain local HTML page (screenshot, controls table, act form, decision buttons) and a `ScriptedOperator` stands in for a person in tests and unattended evidence. The lease, the live-session seam, the evidence and the decision semantics are real and exercised end to end.

## 6. Safety

The allowlist is configuration: hosts (with wildcard), path prefixes, denied route patterns (`/logout`, `/__chaos`), allowed action types. Risk has three classes; a step's declared risk can be raised by policy (by control caption pattern) but never lowered. Handling per class is `allow | require_human | block`; the default is that irreversible actions need a person. Secrets are typed by name and substituted at act time; the redactor runs pattern rules (SSN, card, account number, email, `key=value` secrets) and then known sensitive values over every event, snapshot and result before it touches disk; typed text into password controls is never logged even in discovery; parameter values are never serialized into artifacts.

Limits: the redactor is pattern-based and will miss PII it has no pattern for; the irreversibility classifier is caption-based, so a "Next" button that actually posts would be missed unless the artifact marks it; the allowlist is enforced at navigation and per-action, not at the network layer; and the model still sees non-secret screen content, which in production would need a data-handling agreement with the model provider or an on-prem model.

## 7. Cuts

Cut deliberately: a desktop surface (designed, not built); a real-time co-browsing console (the seam is real, the page is minimal); an artifact diff/review UI (versions and fingerprints exist, the diff is `git diff`); assisted LLM fallback on replay failure (the catalog returns `NEEDS_HUMAN`; a bounded, policy-checked single-step recovery would plug in at `_escalate` and be recorded as a new artifact version); auto-generating tenant overlays (hand-authored today); code generation from artifacts; per-tenant version pinning. The stability runner is thin.

Next with more time, in order: the desktop surface via Windows UIA to prove the seam; overlay generation by replaying on a new tenant, collecting `drift` and `TARGET_NOT_FOUND` anchors, and proposing remaps for review; assisted fallback as a fourth escalation target; and a confidence score per artifact fed by replay history.
