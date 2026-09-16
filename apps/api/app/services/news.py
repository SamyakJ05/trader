"""Corporate announcements and financial headlines, for the AI analyst.

Sources are official or published RSS. Deliberately not a social scraper: an
adversarial feed -- one where people profit precisely when someone trades on
what they post -- is the worst possible input to a trading system, and a
scraper driving a platform's private endpoints with a personal login breaks
without warning and risks the account it logs in as.

Everything here is ADVISORY. The only reader is the analyst's get_news tool,
and the analyst produces proposals a human approves. Two properties make that
necessary rather than cautious:

  * a feed that breaks goes QUIET. "No bad news about this holding" and "the
    fetcher has been dead for a week" are the same empty list, and only one
    of them is a reason to hold.
  * a feed is the one input someone outside this system can write to.

So nothing in this module is reachable from a strategy or the order pipeline.

Parsing is stdlib ElementTree rather than feedparser: RSS is small, regular
XML, and this is one fewer dependency to keep current in an image that places
real orders.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import MarketInstrument, NewsItem, Position

logger = get_logger(__name__)

# Deliberately short. A headline is only ever context for a human decision
# here, and a slow feed must not hold up the worker tick behind it.
_TIMEOUT = httpx.Timeout(10.0)

# Items older than this are not inserted. A month-old headline retrieved
# because a feed re-published it would look current to a reader who sees only
# "published_at desc".
_MAX_AGE = timedelta(days=7)


@dataclass(frozen=True)
class Feed:
    name: str
    url: str


# Verified live when added: each returns current items, not an archive.
#
# Moneycontrol's feeds are deliberately absent. They still answer 200 with
# well-formed RSS, but every one of them -- marketreports, business,
# latestnews, results, buzzingstocks -- is frozen at April 2024. A feed that
# serves stale content successfully is worse than one that fails, because
# nothing downstream can tell; only the staleness filter caught it. Business
# Standard's RSS answers 403 to a plain client.
FEEDS: list[Feed] = [
    Feed(
        "economic_times_markets",
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    ),
    Feed(
        "economic_times_stocks",
        "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    ),
    Feed(
        "hindu_businessline_markets",
        "https://www.thehindubusinessline.com/markets/feeder/default.rss",
    ),
    Feed("livemint_markets", "https://www.livemint.com/rss/markets"),
]


def _parse_published(raw: str | None) -> datetime | None:
    """RFC 822 date from an RSS pubDate.

    Returns None rather than now() on a malformed date. Stamping an
    unparseable item with the current time would promote it to the top of a
    recency-ordered list, which is exactly backwards for the item we
    understand least.
    """
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        # A feed that omits the offset is assumed UTC; guessing IST would
        # shift every item by 5.5 hours in the recency ordering.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# Words that carry no identifying power on their own. "Tata Motors" and "Tata
# Steel" are different companies, so a name stripped to "TATA" would tag every
# group headline to whichever holding matched first.
_GENERIC_NAME_WORDS = frozenset(
    {
        "LIMITED", "LTD", "INDIA", "INDIAN", "COMPANY", "CORPORATION", "CORP",
        "INDUSTRIES", "ENTERPRISES", "GROUP", "THE", "AND", "OF", "CO",
    }
)
# A single short token matches too much prose to be trusted alone.
_MIN_ALIAS_LENGTH = 4


def aliases_for(symbol: str, name: str | None) -> set[str]:
    """Strings in a headline that identify this holding.

    The broker code alone is not enough, and this was the whole reason the
    first version tagged nothing: press writes "Tata Motors" and "NTPC Green
    Energy", never "TATSTE" or "NTPGRE". Broker codes are compressions that
    appear nowhere in prose.

    So the company name is used too, minus the corporate-form words that carry
    no identifying power -- "Limited", "Industries", "Group" -- which would
    otherwise match every company in the market.
    """
    found = {symbol.upper()}
    if name:
        cleaned = re.sub(r"[^A-Z0-9 ]", " ", name.upper())
        words = [
            w for w in cleaned.split() if w not in _GENERIC_NAME_WORDS and len(w) > 1
        ]
        # The full cleaned name, and its leading pair ("TATA MOTORS",
        # "NTPC GREEN"), which is how press usually refers to a company.
        if words:
            found.add(" ".join(words))
        if len(words) >= 2:
            found.add(" ".join(words[:2]))
    # Drop anything too short to be distinctive on its own.
    return {a for a in found if len(a) >= _MIN_ALIAS_LENGTH}


def match_symbol(text: str, aliases: dict[str, set[str]]) -> str | None:
    """The held symbol a headline is about, or None.

    `aliases` maps a symbol to the strings that identify it -- see
    aliases_for. Whole-word matching only: a substring match is not merely
    imprecise here, since "NTPC" occurs inside "NTPGRE" and several NSE codes
    occur inside ordinary English. Attaching a headline to the wrong holding
    is worse than attaching it to none, because the analyst would reason about
    a company the article never mentioned.

    The LONGEST matching alias wins, so "Tata Motors" beats a bare "TATA" and
    a headline naming both NTPC and NTPC Green is filed under the latter.
    """
    upper = text.upper()
    best: tuple[int, str] | None = None
    for symbol, candidates in aliases.items():
        for alias in candidates:
            if re.search(rf"\b{re.escape(alias)}\b", upper) and (
                best is None or len(alias) > best[0]
            ):
                best = (len(alias), symbol)
    return best[1] if best else None


async def held_aliases(db: AsyncSession) -> dict[str, set[str]]:
    """Identifying strings for each held symbol.

    Only holdings: tagging every NSE symbol would attach headlines to
    companies the user has no exposure to, and the analyst's job is this
    account.
    """
    held = {
        s.upper()
        for s in (
            await db.execute(select(Position.symbol).where(Position.quantity != 0))
        )
        .scalars()
        if s
    }
    if not held:
        return {}
    names = dict(
        (
            await db.execute(
                select(MarketInstrument.symbol, MarketInstrument.name).where(
                    MarketInstrument.symbol.in_(held)
                )
            )
        ).all()
    )
    return {symbol: aliases_for(symbol, names.get(symbol)) for symbol in held}


def parse_feed(xml: str, source: str, aliases: dict[str, set[str]]) -> list[dict]:
    """RSS XML to rows. Malformed items are skipped, never guessed at."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as e:
        logger.warning("news.feed_unparseable", source=source, error=str(e))
        return []

    now = datetime.now(timezone.utc)
    items: list[dict] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        published = _parse_published(item.findtext("pubDate"))
        # All three are required. An item with no link cannot be deduplicated
        # and an item with no date cannot be ordered, so neither can be shown
        # to the analyst honestly.
        if not title or not url or published is None:
            continue
        if now - published > _MAX_AGE:
            continue
        summary = (item.findtext("description") or "").strip() or None
        items.append(
            {
                "source": source,
                "symbol": match_symbol(f"{title} {summary or ''}", aliases),
                "title": title[:2000],
                "url": url,
                "summary": summary[:4000] if summary else None,
                "published_at": published,
            }
        )
    return items


async def fetch_feed(
    client: httpx.AsyncClient, feed: Feed, aliases: dict[str, set[str]]
) -> list[dict]:
    """One feed. A failure is logged and yields nothing.

    Never raises: one unreachable publisher must not stop the others, and news
    is advisory, so its absence is not an error condition for the platform.
    """
    try:
        response = await client.get(feed.url, timeout=_TIMEOUT)
        response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        logger.warning("news.fetch_failed", source=feed.name, error=str(e))
        return []
    return parse_feed(response.text, feed.name, aliases)


async def refresh(db: AsyncSession) -> int:
    """Pull every feed and store what is new. Returns rows inserted."""
    aliases = await held_aliases(db)
    rows: list[dict] = []
    # follow_redirects: these publishers redirect http->https and between
    # www hosts, and a 301 would otherwise read as an empty feed.
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for feed in FEEDS:
            rows.extend(await fetch_feed(client, feed, aliases))

    if not rows:
        return 0

    # ON CONFLICT DO NOTHING on the url: feeds republish constantly, and the
    # alternative -- read-then-insert -- races against a concurrent tick and
    # would raise on the unique constraint.
    inserted = 0
    for row in rows:
        result = await db.execute(
            pg_insert(NewsItem).values(**row).on_conflict_do_nothing(index_elements=["url"])
        )
        inserted += result.rowcount or 0
    await db.commit()
    logger.info("news.refreshed", fetched=len(rows), inserted=inserted)
    return inserted


async def recent(
    db: AsyncSession, *, symbol: str | None = None, limit: int = 20
) -> list[NewsItem]:
    """Recent items, optionally for one symbol.

    A symbol query returns that symbol's items AND untagged market-wide ones,
    because a market-wide headline is context for any holding.
    """
    query = select(NewsItem).order_by(NewsItem.published_at.desc()).limit(limit)
    if symbol:
        query = query.where(
            (NewsItem.symbol == symbol.upper()) | (NewsItem.symbol.is_(None))
        )
    return list((await db.execute(query)).scalars())
