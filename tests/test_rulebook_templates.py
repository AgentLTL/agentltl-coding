"""examples/rulebooks/*.yaml: the domain templates, with their tools renamed to MCP names.

Each template must be as strict as it says: a gate holds for every action, roles are not
interchangeable, a region outside the allow-list (or none) is refused."""

from pathlib import Path

import pytest

from agentltl_coding.guard import lint

from .conftest import guard_for, run

DIR = Path(__file__).parent.parent / "examples" / "rulebooks"


def tool(server, name, **args):
    return (f"mcp__{server}__{name}", args)


def bank(name, **args):
    return tool("bank", name, **args)


def clinic(name, **args):
    return tool("clinic", name, **args)


def erp(name, **args):
    return tool("erp", name, **args)


def desk(name, **args):
    return tool("helpdesk", name, **args)


def data(name, **args):
    return tool("data", name, **args)


def guard(template):
    return guard_for((DIR / f"{template}.yaml").read_text())


def test_the_templates_are_all_here():
    assert sorted(p.stem for p in DIR.glob("*.yaml")) == [
        "healthcare", "payments", "privacy", "procurement", "support"]


@pytest.mark.parametrize("template", sorted(p.stem for p in DIR.glob("*.yaml")))
def test_a_template_lints_clean(template):
    g = guard(template)
    assert lint(g.ruleset) == []
    assert g.ruleset.rules and all(r.why for r in g.ruleset.rules)


CHECKS = [bank("verify_identity"), bank("check_sanctions"), bank("match_invoice")]
APPROVALS = [bank("approve_payment", role="finance"), bank("approve_payment", role="manager")]
PAY = bank("execute_payment")

# template, steps, the action each one gets
CASES = [
    # payments
    ("payments", CHECKS + APPROVALS + [PAY], ["none"] * 6),
    ("payments", [PAY], ["deny"]),
    ("payments", CHECKS + [APPROVALS[0], APPROVALS[0], PAY], ["none"] * 5 + ["deny"]),
    ("payments", CHECKS + [APPROVALS[1], APPROVALS[1], PAY], ["none"] * 5 + ["deny"]),
    ("payments", CHECKS[:2] + APPROVALS + [PAY], ["none"] * 4 + ["deny"]),
    # a second payment needs its own checks and approvals, and is refused anyway (one per session)
    ("payments", CHECKS + APPROVALS + [PAY, PAY], ["none"] * 6 + ["deny"]),
    ("payments", CHECKS + APPROVALS + [PAY] + CHECKS + APPROVALS + [PAY],
     ["none"] * 6 + ["none"] * 5 + ["deny"]),
    # the call-back must come after the beneficiary was added
    ("payments", [bank("add_beneficiary")] + CHECKS + APPROVALS + [PAY],
     ["none"] * 6 + ["deny"]),
    ("payments", [bank("callback_verification"), bank("add_beneficiary")] + CHECKS + APPROVALS
     + [PAY], ["none"] * 7 + ["deny"]),
    ("payments", [bank("add_beneficiary"), bank("callback_verification")] + CHECKS + APPROVALS
     + [PAY], ["none"] * 8),
    ("payments", [bank("add_beneficiary"), bank("callback_verification"), bank("add_beneficiary")]
     + CHECKS + APPROVALS + [PAY], ["none"] * 8 + ["deny"]),
    ("payments", [bank("place_order"), bank("get_quote"), bank("place_order"), bank("get_quote"),
                  bank("check_sanctions"), bank("place_order")],
     ["deny", "none", "none", "none", "none", "deny"]),
    # healthcare
    ("healthcare", [clinic("verify_patient_identity"), clinic("check_allergies"),
                    clinic("check_interactions"), clinic("check_dosage"), clinic("prescribe")],
     ["none"] * 5),
    ("healthcare", [clinic("prescribe")], ["stop"]),
    ("healthcare", [clinic("verify_patient_identity"), clinic("check_allergies"),
                    clinic("check_interactions"), clinic("check_dosage"), clinic("prescribe"),
                    clinic("prescribe")], ["none"] * 5 + ["stop"]),
    ("healthcare", [clinic("verify_patient_identity"), clinic("check_allergies"),
                    clinic("check_interactions"), clinic("check_dosage"),
                    clinic("prescribe_controlled")], ["none"] * 4 + ["ask"]),
    ("healthcare", [clinic("verify_patient_identity"), clinic("check_allergies"),
                    clinic("check_interactions"), clinic("check_dosage"),
                    clinic("physician_approval"), clinic("prescribe_controlled")], ["none"] * 6),
    # procurement
    ("procurement", [erp("check_conflict_of_interest"), erp("check_budget")]
     + [erp("request_quote")] * 3 + [erp("award_contract")], ["none"] * 6),
    ("procurement", [erp("check_conflict_of_interest"), erp("check_budget")]
     + [erp("request_quote")] * 2 + [erp("award_contract")], ["none"] * 4 + ["deny"]),
    ("procurement", [erp("award_contract")], ["stop"]),
    ("procurement", [erp("check_conflict_of_interest"), erp("check_budget")]
     + [erp("request_quote")] * 3 + [erp("award_contract"), erp("award_contract")],
     ["none"] * 6 + ["stop"]),
    # support
    ("support", [desk("get_ticket_history"), desk("reply")], ["none"] * 2),
    ("support", [desk("reply")], ["deny"]),
    ("support", [desk("lookup_customer"), desk("get_order"), desk("issue_refund")], ["none"] * 3),
    ("support", [desk("lookup_customer"), desk("get_order"), desk("issue_refund"),
                 desk("issue_refund")], ["none"] * 3 + ["deny"]),
    ("support", [desk("lookup_customer"), desk("issue_refund")], ["none", "deny"]),
    ("support", [desk("get_ticket_history"), desk("reply"), desk("close_ticket"), desk("reply")],
     ["none"] * 3 + ["deny"]),
    # privacy
    ("privacy", [data("check_consent"), data("process_personal_data")], ["none"] * 2),
    ("privacy", [data("process_personal_data")], ["stop"]),
    ("privacy", [data("check_consent"), data("process_personal_data"),
                 data("process_personal_data")], ["none"] * 2 + ["stop"]),
    ("privacy", [data("anonymize"), data("export_dataset", include_pii=False),
                 data("export_dataset", include_pii=False)], ["none", "none", "deny"]),
    ("privacy", [data("anonymize"), data("export_dataset", include_pii=True)], ["none", "deny"]),
    ("privacy", [data("transfer_data", region="eu-west-1"), data("transfer_data", region="EU"),
                 data("transfer_data", region="europe-west1")], ["none"] * 3),
    ("privacy", [data("transfer_data", region="us-east-1"), data("transfer_data"),
                 data("transfer_data", region="")], ["stop"] * 3),
    ("privacy", [data("share_with_third_party"), data("check_dpa"),
                 data("share_with_third_party")], ["deny", "none", "none"]),
]


@pytest.mark.parametrize("template, steps, expected", CASES,
                         ids=[f"{c[0]}-{i}" for i, c in enumerate(CASES)])
def test_template_behaviour(template, steps, expected):
    assert run(guard(template), *steps) == expected


def test_the_refusal_names_the_missing_role():
    g = guard("payments")
    run(g, *CHECKS, APPROVALS[1], APPROVALS[1])
    reason = g.decide(*PAY).reason
    assert "finance-approval-before-payment" in reason and "finance" in reason


# finally rules: checked when the agent is about to finish
FINISH = [
    ("healthcare", [clinic("verify_patient_identity"), clinic("check_allergies"),
                    clinic("check_interactions"), clinic("check_dosage"), clinic("prescribe")],
     [clinic("update_medical_record")]),
    ("procurement", [erp("accept_gift")], [erp("register_gift")]),
    ("support", [desk("escalate_ticket")], [desk("notify_customer")]),
    ("privacy", [data("read_personal_record")], [data("log_access")]),
    ("privacy", [data("erasure_request")], [data("delete_personal_data")]),
]


@pytest.mark.parametrize("template, owed, fix", FINISH, ids=[f"{t}-{i}" for i, (t, _, _) in enumerate(FINISH)])
def test_an_obligation_is_checked_at_the_end(template, owed, fix):
    g = guard(template)
    assert g.finish().action == "none"
    assert all(a == "none" for a in run(g, *owed))
    assert g.finish().action == "block"
    run(g, *fix)
    g.new_turn()
    assert g.finish().action == "none"
