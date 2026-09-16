"""News feed parsing, symbol matching, and the analyst tool.

Symbol matching is where this goes wrong quietly. A headline attached to the
wrong holding makes the analyst reason about a company the article never
mentioned, and nothing downstream can detect that -- the tool output looks
identical whether the attribution was right or wrong.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services.ai.tools import TOOLS
from app.services.news import (
    _parse_published,
    aliases_for,
    match_symbol,
    parse_feed,
)

BY_NAME = {t["name"]: t for t in TOOLS}

# The user's real holdings, with the names the instrument master carries.
# NTPC/NTPGRE and Tata Steel/Tata Capital are the two collisions that matter.
HOLDINGS = {
    "RELIND": "RELIANCE INDUSTRIES LIMITED",
    "NTPC": "NTPC LIMITED",
    "NTPGRE": "NTPC GREEN ENERGY LIMITED",
    "TATSTE": "TATA STEEL LIMITED",
    "TATCAP": "TATA CAPITAL LIMITED",
    "VEDLIM": "VEDANTA LIMITED",
}
HELD = {symbol: aliases_for(symbol, name) for symbol, name in HOLDINGS.items()}


def rss(items: str) -> str:
    return f"<rss><channel>{items}</channel></rss>"


def item(title, url="https://example.test/a", when=None, description=""):
    when = when or datetime.now(timezone.utc)
    stamp = when.strftime("%a, %d %b %Y %H:%M:%S +0000")
    return (
        f"<item><title>{title}</title><link>{url}</link>"
        f"<pubDate>{stamp}</pubDate><description>{description}</description></item>"
    )


# ── symbol matching ──────────────────────────────────────────────────


def test_the_company_NAME_is_matched_not_only_the_broker_code():
    """The failure the first version had: press writes "Tata Steel", never
    "TATSTE". Matching codes alone tagged 1 headline out of 195 live items,
    and the one it caught was incidental."""
    assert match_symbol("Tata Steel Q2 profit rises 18%", HELD) == "TATSTE"
    assert match_symbol("Vedanta demerger gets approval", HELD) == "VEDLIM"


def test_a_more_specific_company_name_wins_over_a_shorter_one():
    """NTPC Green Energy is a different company from NTPC, and the user holds
    both. Filing its news under NTPC would have the analyst reasoning about
    the wrong balance sheet."""
    assert match_symbol("NTPC Green Energy commissions 200MW solar", HELD) == "NTPGRE"
    assert match_symbol("NTPC board approves dividend", HELD) == "NTPC"


def test_a_group_name_alone_identifies_nothing():
    """"Tata Group" spans Tata Steel and Tata Capital here. Picking either
    would be a coin flip presented to the analyst as fact."""
    assert match_symbol("Tata Group stocks fall 7% today", HELD) is None


def test_an_unheld_company_matches_nothing():
    """Tagging a symbol the user has no exposure to would have the analyst
    reasoning about an irrelevant company."""
    assert match_symbol("Infosys posts strong quarter", HELD) is None


def test_matching_is_case_insensitive():
    assert match_symbol("reliance industries board meeting", HELD) == "RELIND"


def test_a_symbol_inside_an_ordinary_word_is_not_a_match():
    """Several NSE codes are short enough to occur inside English words."""
    assert match_symbol("PRINTPCB maker expands", {"NTPC": {"NTPC"}}) is None


def test_corporate_form_words_are_not_treated_as_identifying():
    """Every company is a "Limited". An alias of LIMITED would tag the whole
    market to whichever holding matched first."""
    aliases = aliases_for("TATSTE", "TATA STEEL LIMITED")
    assert "LIMITED" not in aliases
    assert "TATA STEEL" in aliases


def test_a_short_alias_is_dropped_as_indistinctive():
    """A three-letter token matches too much ordinary prose to trust."""
    assert all(len(a) >= 4 for a in aliases_for("ABB", "ABB INDIA LIMITED"))


# ── date parsing ─────────────────────────────────────────────────────


def test_an_unparseable_date_returns_none_rather_than_now():
    """Stamping a malformed item with the current time would promote it to
    the top of a recency-ordered list -- backwards for the item we understand
    least."""
    assert _parse_published("not a date") is None
    assert _parse_published(None) is None
    assert _parse_published("") is None


def test_a_naive_date_is_treated_as_utc():
    parsed = _parse_published("Mon, 01 Jan 2029 10:00:00")
    assert parsed is not None
    assert parsed.tzinfo is not None


# ── feed parsing ─────────────────────────────────────────────────────


def test_a_well_formed_item_is_parsed():
    rows = parse_feed(rss(item("RELIND announces buyback")), "test", HELD)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "RELIND"
    assert rows[0]["source"] == "test"


def test_items_missing_a_link_or_date_are_skipped_not_guessed():
    """An item with no link cannot be deduplicated and one with no date
    cannot be ordered, so neither can be shown to the analyst honestly."""
    no_link = "<item><title>T</title><pubDate>Mon, 01 Jan 2029 10:00:00 +0000</pubDate></item>"
    no_date = "<item><title>T</title><link>https://example.test/x</link></item>"
    assert parse_feed(rss(no_link + no_date), "test", HELD) == []


def test_stale_items_are_dropped():
    """A feed re-publishing an old headline would otherwise look current."""
    old = item("RELIND old news", when=datetime.now(timezone.utc) - timedelta(days=30))
    assert parse_feed(rss(old), "test", HELD) == []


def test_malformed_xml_yields_nothing_rather_than_raising():
    """A publisher serving an error page as XML must not break the tick."""
    assert parse_feed("<rss><channel><item>", "test", HELD) == []
    assert parse_feed("not xml at all", "test", HELD) == []


def test_a_market_wide_headline_has_no_symbol():
    """Attaching it to an arbitrary holding would invent a relationship."""
    rows = parse_feed(rss(item("Sensex closes higher")), "test", HELD)
    assert rows[0]["symbol"] is None


# ── the analyst tool ─────────────────────────────────────────────────


def test_the_tool_is_registered():
    assert "get_news" in BY_NAME


def test_symbol_is_optional():
    """Market-wide context is a legitimate query on its own."""
    assert BY_NAME["get_news"]["input_schema"].get("required") in (None, [])


def test_the_description_says_empty_is_not_evidence_of_safety():
    """The property that makes this advisory-only. A dead fetcher and a quiet
    news day produce the same empty list, and a model that reads absence as
    reassurance would recommend holding on the strength of a bug."""
    description = BY_NAME["get_news"]["description"]
    assert "never evidence" in description
    assert "outage" in description


def test_the_description_rules_out_trading_on_news_alone():
    assert "never as the sole reason" in BY_NAME["get_news"]["description"]


async def test_the_tool_caps_an_unbounded_limit(monkeypatch):
    """An unbounded limit would crowd the context window with headlines and
    push out the position and risk data the decision rests on."""
    from types import SimpleNamespace

    from app.services.ai import tools as tools_module

    seen = {}

    async def fake_recent(db, *, symbol=None, limit=20):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(tools_module.news_service, "recent", fake_recent)
    out = json.loads(
        await tools_module.run_tool(
            None,
            None,
            None,
            SimpleNamespace(environment="live", broker="icici_breeze", id=None),
            "get_news",
            {"limit": 100000},
        )
    )
    assert seen["limit"] == 50
    assert out["items"] == []


@pytest.mark.parametrize("kind", ["sma_crossover", "rsi_mean_reversion", "atr_channel"])
def test_no_strategy_can_read_news(kind):
    """The structural guarantee. News is advisory, and a strategy trades
    unattended -- so a strategy must have no path to this data at all."""
    import inspect

    from app.engines.strategy.runner import STRATEGY_REGISTRY

    source = inspect.getsource(type(STRATEGY_REGISTRY[kind]))
    assert "news" not in source.lower()
