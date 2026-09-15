"""Which price feed a strategy reads.

A live strategy trading off simulated candles is worse than one with no data:
it places real orders against invented prices and looks like it is working.
The feed follows the account's environment rather than the strategy's params,
so the two cannot disagree.
"""

from app.workers.tick_stream import LIVE_SOURCE


def resolve_source(environment: str, params: dict) -> str | None:
    """Mirrors the runner's choice, so the rule is testable in isolation.

    Returns None where the runner refuses to run the strategy at all.
    """
    is_live = environment == "live"
    source = params.get("source") or (LIVE_SOURCE if is_live else "simulator")
    if is_live and source == "simulator":
        return None
    return source


def test_a_paper_strategy_reads_the_simulator():
    assert resolve_source("paper", {}) == "simulator"


def test_a_live_strategy_reads_the_live_feed():
    assert resolve_source("live", {}) == LIVE_SOURCE


def test_a_live_strategy_pinned_to_the_simulator_is_refused():
    """The dangerous case: params saying 'simulator' on a live account would
    place real orders against invented prices."""
    assert resolve_source("live", {"source": "simulator"}) is None


def test_an_explicit_source_is_honoured_on_paper():
    """Backtesting against imported history is a legitimate paper config."""
    assert resolve_source("paper", {"source": "yfinance_unadjusted"}) == "yfinance_unadjusted"


def test_a_live_strategy_may_name_a_non_simulated_source():
    """Only the simulator is refused; a live account on real imported data is
    unusual but not dangerous in the same way."""
    assert resolve_source("live", {"source": "yfinance_unadjusted"}) == "yfinance_unadjusted"


def test_the_live_source_is_distinct_from_the_simulator():
    """If these ever collided, the refusal above would silently stop firing."""
    assert LIVE_SOURCE != "simulator"


# ── each broker's live candles are its own ───────────────────────────


def test_each_broker_tags_its_live_candles_differently():
    """Two brokers do not use the same code for the same instrument — Breeze's
    RELIND is Kite's RELIANCE — so their candles must never merge into one
    series. Distinct source tags are what keeps them apart."""
    from app.workers.tick_stream import LIVE_SOURCES

    assert LIVE_SOURCES["zerodha"] != LIVE_SOURCES["icici_breeze"]
    assert "simulator" not in LIVE_SOURCES.values()


def test_a_live_breeze_strategy_reads_breeze_candles():
    from app.workers.tick_stream import LIVE_SOURCE, LIVE_SOURCES

    def resolve(broker, environment, params):
        is_live = environment == "live"
        source = params.get("source") or (
            LIVE_SOURCES.get(broker, LIVE_SOURCE) if is_live else "simulator"
        )
        return None if (is_live and source == "simulator") else source

    assert resolve("icici_breeze", "live", {}) == "breeze"
    assert resolve("zerodha", "live", {}) == "kite"
    assert resolve("icici_breeze", "paper", {}) == "simulator"
