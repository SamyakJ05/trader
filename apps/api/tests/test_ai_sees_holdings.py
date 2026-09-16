"""The analyst has to be able to see stock the user actually owns.

get_positions reads the Position table, which holds intraday and F&O exposure.
Settled demat stock lives in holdings_snapshots and was reachable by no tool at
all -- so an account holding fifteen scrips reported nothing, and the analyst
concluded the user owned nothing. That is the worst shape this can fail in: not
an error, a confident wrong answer it then reasons from.
"""

from app.services.ai.tools import TOOLS

TOOL_NAMES = {t["name"] for t in TOOLS}


def test_a_holdings_tool_exists():
    assert "get_holdings" in TOOL_NAMES


def test_positions_and_holdings_are_separate_tools():
    """Folding holdings into get_positions would be wrong, not just untidy:
    the two answer different questions and only one is sellable today."""
    assert "get_positions" in TOOL_NAMES


def test_the_description_warns_that_positions_excludes_holdings():
    """The model decides which tool to call from the description alone. Left
    to infer, it calls get_positions, gets an empty list, and stops -- which
    is the bug this tool was added to fix."""
    holdings = next(t for t in TOOLS if t["name"] == "get_holdings")
    description = holdings["description"]
    assert "get_positions" in description, (
        "the description must say get_positions excludes holdings, or the "
        "model has no reason to call this tool"
    )
    # The empty-positions case is exactly when the model is most likely to
    # conclude the account is empty.
    assert "zero positions" in description
