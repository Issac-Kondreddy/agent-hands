"""
Hand-built reference capability for the Meridian target app.

This mirrors what the recorder emits after a discovery run, but is kept
independent of any model so the replay engine, condition rules, handoff and
overlays can be tested deterministically without an API key.
"""
from agent_hands.schema import (ActionType, Anchor, Capability, Checkpoint, ConditionKind, ConditionMatch,
                                ConditionResponse, ConditionRule, Expectation, Output, OutcomeSpec, Parameter,
                                ParamType, ResponseKind, RiskClass, Step, TableCell)


SIGNED_ON = Expectation(role_visible=Anchor(anchor_id="lookup_menu_link", role="link", name="Member Lookup", frame=["menu"]))


def login_steps(prefix: str = "login") -> list[Step]:
    """Sign-on steps. Each is skipped when the session is already signed on (idempotent)."""
    return [
        Step(step_id=f"{prefix}_user", action=ActionType.TYPE, risk=RiskClass.REVERSIBLE, skip_if=SIGNED_ON,
             target=Anchor(anchor_id="operator_id_input", role="textbox", name="Operator ID", label="Operator ID"),
             value="${secret:MERIDIAN_DEMO_USER}", description="Enter operator id"),
        Step(step_id=f"{prefix}_pass", action=ActionType.TYPE, risk=RiskClass.REVERSIBLE, skip_if=SIGNED_ON,
             target=Anchor(anchor_id="password_input", role="textbox", name="Password", label="Password"),
             value="${secret:MERIDIAN_DEMO_PASS}", description="Enter password (secret, never logged)"),
        Step(step_id=f"{prefix}_submit", action=ActionType.CLICK, risk=RiskClass.REVERSIBLE, skip_if=SIGNED_ON,
             target=Anchor(anchor_id="sign_on_button", role="button", name="Sign On"),
             expect=SIGNED_ON, description="Sign on"),
    ]


def savings_balance_capability(entry: str) -> Capability:
    return Capability(
        capability_id="meridian_core.member_savings_balance",
        version=1, status="approved",
        title="Read a member's regular savings balance",
        description="Looks up a member by number and returns the current balance of their regular savings account.",
        app_id="meridian_core", surface="legacy_web", entry_point=entry, recorded_on_tenant="meridian",
        parameters=[Parameter(name="member_id", type=ParamType.STRING, pattern=r"\d{5}",
                              description="5-digit member number", example="12345")],
        outputs=[Output(name="member_name", description="Member's full name"),
                 Output(name="savings_balance", type=ParamType.MONEY, description="Current regular savings balance",
                        post_process=r"\$?([\d,]+\.\d{2})")],
        outcomes=[OutcomeSpec(code="MEMBER_NOT_FOUND", description="No member exists with that number"),
                  OutcomeSpec(code="ACCESS_DENIED", description="Operator role may not view this member")],
        preconditions=["Operator credentials available as secrets MERIDIAN_DEMO_USER / MERIDIAN_DEMO_PASS"],
        steps=[
            *login_steps(),
            Step(step_id="open_lookup", action=ActionType.CLICK,
                 target=Anchor(anchor_id="lookup_menu_link", role="link", name="Member Lookup", frame=["menu"]),
                 expect=Expectation(role_visible=Anchor(anchor_id="member_number_input", role="textbox", name="Member Number", frame=["main"])),
                 description="Open Member Lookup"),
            Step(step_id="enter_member", action=ActionType.TYPE, risk=RiskClass.REVERSIBLE,
                 target=Anchor(anchor_id="member_number_input", role="textbox", name="Member Number", label="Member Number", frame=["main"]),
                 value="${member_id}", description="Enter the member number"),
            Step(step_id="search", action=ActionType.CLICK, risk=RiskClass.REVERSIBLE,
                 target=Anchor(anchor_id="search_button", role="button", name="Search", frame=["main"]),
                 expect=Expectation(text_visible="Member Detail"), description="Search"),
            Step(step_id="read_name", action=ActionType.READ, output="member_name",
                 target=Anchor(anchor_id="member_name_cell", role="cell", label="Name", frame=["main"],
                               rationale="Value cell immediately right of the bold 'Name:' label cell"),
                 description="Read the member's name"),
            Step(step_id="read_balance", action=ActionType.READ, output="savings_balance",
                 target=Anchor(anchor_id="savings_balance_cell", role="cell", frame=["main"],
                               table_cell=TableCell(row_header="Regular Savings", row_header_column="Account Type",
                                                    column_header="Current Balance")),
                 description="Read the regular savings balance"),
        ],
        checkpoint=Checkpoint(description="Member detail screen shown with an Accounts table",
                              expect=Expectation(text_visible="Current Balance")),
        conditions=[
            ConditionRule(rule_id="not_found", kind=ConditionKind.BUSINESS_OUTCOME,
                          match=ConditionMatch(text_pattern=r"No record found for [^.]*"),
                          response=ConditionResponse(kind=ResponseKind.RETURN_OUTCOME, outcome_code="MEMBER_NOT_FOUND",
                                                     message_capture=r"(No record found for [^.]*)"),
                          applies_after_steps=["search"], description="Legitimate answer: member does not exist"),
            ConditionRule(rule_id="access_denied", kind=ConditionKind.BUSINESS_OUTCOME,
                          match=ConditionMatch(text_pattern=r"Access denied"),
                          response=ConditionResponse(kind=ResponseKind.RETURN_OUTCOME, outcome_code="ACCESS_DENIED"),
                          applies_after_steps=["search"]),
            ConditionRule(rule_id="maintenance_notice", kind=ConditionKind.RECOVERABLE,
                          match=ConditionMatch(text_pattern=r"System Notice"),
                          response=ConditionResponse(kind=ResponseKind.DISMISS, max_attempts=2,
                                                     dismiss_target=Anchor(anchor_id="ack_button", role="button", name="Acknowledge")),
                          description="Once-per-session interstitial; acknowledge and continue"),
            ConditionRule(rule_id="session_expired", kind=ConditionKind.RECOVERABLE,
                          match=ConditionMatch(text_pattern=r"Session Expired"),
                          response=ConditionResponse(kind=ResponseKind.RUN_RECOVERY, recovery_block="re_signon",
                                                     resume_from_step="enter_member", max_attempts=1),
                          description="Operator session timed out; sign on again and resume"),
        ],
        recovery_blocks={"re_signon": [
            Step(step_id="rs_link", action=ActionType.CLICK,
                 target=Anchor(anchor_id="return_to_signon", role="link", name="Return to Sign On"),
                 expect=Expectation(role_visible=Anchor(anchor_id="sign_on_button", role="button", name="Sign On"))),
            *login_steps("rs"),
            Step(step_id="rs_lookup", action=ActionType.CLICK,
                 target=Anchor(anchor_id="lookup_menu_link", role="link", name="Member Lookup", frame=["menu"]),
                 expect=Expectation(role_visible=Anchor(anchor_id="member_number_input", role="textbox", name="Member Number", frame=["main"]))),
        ]},
    )


def open_subaccount_capability(entry: str) -> Capability:
    """A flow with an irreversible commit step — exercises human approval."""
    base = savings_balance_capability(entry)
    steps = [s for s in base.steps if not s.step_id.startswith("read_")]
    steps += [
        Step(step_id="open_form", action=ActionType.CLICK, risk=RiskClass.REVERSIBLE,
             target=Anchor(anchor_id="open_sub_button", role="button", name="Open Sub-Account", frame=["main"]),
             expect=Expectation(text_visible="Account Type")),
        Step(step_id="nickname", action=ActionType.TYPE, risk=RiskClass.REVERSIBLE,
             target=Anchor(anchor_id="nickname_input", role="textbox", name="Account Nickname", frame=["main"]),
             value="${nickname}"),
        Step(step_id="deposit", action=ActionType.TYPE, risk=RiskClass.REVERSIBLE,
             target=Anchor(anchor_id="deposit_input", role="textbox", name="Initial Deposit", frame=["main"]),
             value="${deposit}"),
        Step(step_id="continue", action=ActionType.CLICK, risk=RiskClass.REVERSIBLE,
             target=Anchor(anchor_id="continue_button", role="button", name="Continue", frame=["main"]),
             expect=Expectation(text_visible="Review New Sub-Account")),
        Step(step_id="confirm", action=ActionType.CLICK, risk=RiskClass.IRREVERSIBLE, requires_human_approval=True,
             target=Anchor(anchor_id="confirm_button", role="button", name="Confirm & Open", frame=["main"]),
             expect=Expectation(text_visible="Sub-Account Opened"), description="Commit to the core ledger"),
        Step(step_id="read_new_no", action=ActionType.READ, output="new_account_number",
             target=Anchor(anchor_id="new_account_cell", role="cell", label="New Account Number", frame=["main"]),
             description="Read the new account number"),
    ]
    return Capability.model_validate(base.model_copy(update={
        "capability_id": "meridian_core.open_savings_subaccount", "title": "Open a savings sub-account",
        "description": "Opens a new regular-savings sub-account for a member with a nickname and initial deposit.",
        "parameters": base.parameters + [Parameter(name="nickname", description="Display name for the new account"),
                                         Parameter(name="deposit", type=ParamType.MONEY, description="Initial deposit, USD")],
        "outputs": [Output(name="new_account_number", description="Account number assigned by the core")],
        "outcomes": base.outcomes + [OutcomeSpec(code="VALIDATION_ERROR", description="The core rejected the form input")],
        "steps": steps,
        "checkpoint": Checkpoint(description="Confirmation screen", expect=Expectation(text_visible="opened successfully")),
        "conditions": base.conditions + [
            ConditionRule(rule_id="validation", kind=ConditionKind.BUSINESS_OUTCOME,
                          match=ConditionMatch(text_pattern=r"must be at least \$[\d.]+|is required\.|must be a number"),
                          response=ConditionResponse(kind=ResponseKind.RETURN_OUTCOME, outcome_code="VALIDATION_ERROR",
                                                     message_capture=r"((?:[A-Z][^.]*?)(?:must be at least \$[\d.]+|is required|must be a number)\.)"),
                          applies_after_steps=["continue"])],
    }).model_dump())
