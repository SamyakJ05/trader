"""The analyst's recall of its own past decisions.

Deliberately recall, not learned belief: every field here is a record the
database already holds, so the analyst cannot be wrong about what happened. A
scratchpad the model wrote to itself would have the opposite property -- a
wrong conclusion written once is read back every day afterwards, reinforcing
itself, and nothing downstream can tell a wrong note from a right one.

What these pin is mostly FRAMING. An empty result and a small sample are the
two things a model reads as reassurance when they are nothing of the kind, and
this account has almost no trading history yet.
"""

from app.services.ai.tools import TOOLS

BY_NAME = {t["name"]: t for t in TOOLS}


def test_the_recall_tools_are_registered():
    assert "get_past_proposals" in BY_NAME
    assert "get_trade_outcomes" in BY_NAME


def test_past_proposals_tells_the_model_to_check_before_re_proposing():
    """The genuinely useful signal here is the user's REJECTIONS. Without
    this instruction the analyst proposes the same declined trade again, and
    the user has to decline it again."""
    description = BY_NAME["get_past_proposals"]["description"]
    assert "REJECTED" in description
    assert "before proposing" in description


def test_past_proposals_says_empty_is_not_success():
    """An account that has never proposed anything and one whose proposals
    all worked produce the same empty list."""
    assert "NOT that past proposals all succeeded" in (
        BY_NAME["get_past_proposals"]["description"]
    )


def test_outcomes_warns_that_a_small_sample_is_not_evidence():
    """The core risk of giving a model its own track record. Three profitable
    trades is noise, and a model told only 'here are your outcomes' will
    report them as a pattern."""
    description = BY_NAME["get_trade_outcomes"]["description"]
    assert "noise" in description
    assert "do not infer" in description.lower()


def test_strategies_explains_that_zero_orders_is_not_failure():
    """A RUNNING strategy that has never fired looks identical to a working
    one from the status alone, and the difference matters: one is waiting for
    a condition, the other may have a condition that can never be met."""
    description = BY_NAME["get_strategies"]["description"]
    assert "zero orders" in description.lower()


def test_no_tool_lets_the_model_write_its_own_conclusions():
    """The structural guarantee of recall-only. propose_trade writes a
    proposal the user approves; nothing else writes anything the model will
    later read back as fact."""
    writers = [
        name
        for name in BY_NAME
        if name.startswith(("set_", "save_", "remember_", "note_", "write_"))
    ]
    assert writers == []


async def test_outcomes_reports_only_proposals_that_became_orders(monkeypatch):
    """A rejected proposal has no outcome to report. Including it with null
    fields would read as a trade that went nowhere rather than one the user
    declined -- which is what get_past_proposals is for."""
    import inspect

    from app.services.ai import tools as tools_module

    source = inspect.getsource(tools_module.run_tool)
    outcomes = source.split('if name == "get_trade_outcomes":')[1].split(
        'if name == "get_orders":'
    )[0]
    # An inner join on order_id: a proposal with no order cannot appear.
    assert ".join(Order, AIProposal.order_id == Order.id)" in outcomes


def test_outcomes_labels_pnl_as_per_symbol_not_per_trade():
    """Realized P&L is a position-level figure. Presented per proposal without
    that caveat, the same rupees would be attributed to every trade in the
    symbol and the analyst would multiply its own success."""
    assert "per SYMBOL, not per trade" in BY_NAME["get_trade_outcomes"]["description"]
