"""Broker capability matrix.

Two distinct axes — do not conflate them:
- `capabilities`: what the BROKER's API supports, per public docs.
- `adapter_status`: how far OUR adapter implementation has actually gotten.

A capability being True does NOT mean our adapter implements it. The UI
shows both. Verify against current broker docs before flipping anything
to live; broker APIs change.
"""

from pydantic import BaseModel

from app.domain.enums import AdapterStatus, Broker


class BrokerCapabilities(BaseModel):
    broker: Broker
    adapter_status: AdapterStatus
    display_name: str
    auth_model: str
    session_validity: str
    rate_limit_notes: str
    place_order: bool
    modify_order: bool
    cancel_order: bool
    holdings: bool
    positions: bool
    funds: bool
    instruments_dump: bool
    websocket_ticks: bool
    order_postbacks: bool
    amo_orders: bool
    bracket_gtt: bool
    exchanges: list[str]
    notes: str = ""


# Adapters whose order path has been exercised against the real broker, with
# what was verified and when. An entry is a claim that an order was ACCEPTED,
# not that the payloads look right -- scaffold already means that.
#
# Both tripwires read this: the CI step in .github/workflows/ci.yml, which
# fails any build marking a non-paper adapter working without an entry here,
# and test_live_gate, which asserts the same thing plus its converse. One
# source so the two cannot disagree about what has been verified.
#
#   icici_breeze — 2026-09-17, CASH EQUITY ONLY. RELIND (ISIN INE002A01018),
#   1 share, LIMIT well below market, CNC, from the registered static IP
#   during market hours. F&O and every product other than CNC are still
#   unexercised. See docs/breeze-verification-playbook.md stage 5.
VERIFIED_LIVE_ADAPTERS: frozenset[Broker] = frozenset({Broker.ICICI_BREEZE})


CAPABILITY_MATRIX: dict[Broker, BrokerCapabilities] = {
    Broker.PAPER: BrokerCapabilities(
        broker=Broker.PAPER,
        adapter_status=AdapterStatus.WORKING,
        display_name="Paper Simulator",
        auth_model="none (internal)",
        session_validity="n/a",
        rate_limit_notes="n/a",
        place_order=True,
        modify_order=True,
        cancel_order=True,
        holdings=True,
        positions=True,
        funds=True,
        instruments_dump=False,
        websocket_ticks=False,
        order_postbacks=False,
        amo_orders=False,
        bracket_gtt=False,
        exchanges=["NSE", "BSE", "NFO"],
        notes="Simulated fills with statutory charges, cash ledger and T+1 delivery holdings.",
    ),
    Broker.ZERODHA: BrokerCapabilities(
        broker=Broker.ZERODHA,
        adapter_status=AdapterStatus.SCAFFOLD,
        display_name="Zerodha Kite Connect",
        auth_model="api_key + daily login redirect -> request_token -> "
        "SHA-256 checksum exchange for access_token",
        session_validity="access_token expires daily (~6am IST flush); re-login every day",
        rate_limit_notes="~3 req/s per app for most endpoints, 10 req/s quotes, "
        "order placement bursts limited; see Kite docs",
        place_order=True,
        modify_order=True,
        cancel_order=True,
        holdings=True,
        positions=True,
        funds=True,
        instruments_dump=True,
        websocket_ticks=True,
        order_postbacks=True,
        # Kite supports AMO; this adapter cannot send one. Every order path
        # hardcodes variety=regular, and OrderRequest has no variety field to
        # ask for anything else. An AMO sent as regular is rejected outside
        # market hours and executes immediately inside them -- a different
        # order from the one intended -- so the flag must not advertise it.
        amo_orders=False,
        # Same: Kite supports both, this adapter implements neither.
        bracket_gtt=False,
        exchanges=["NSE", "BSE", "NFO", "BFO", "MCX", "CDS"],
        notes="Adapter scaffolded against documented Kite Connect v3 REST endpoints. "
        "NOT verified against a live account. WebSocket ticks ARE implemented "
        "(see ticker.py and tick_feed) but likewise unverified; AMO, bracket "
        "and GTT orders are not implemented at all.",
    ),
    Broker.GROWW: BrokerCapabilities(
        broker=Broker.GROWW,
        adapter_status=AdapterStatus.SCAFFOLD,
        display_name="Groww Trading API",
        auth_model="API key + secret (TOTP-based token generation) or direct daily access token",
        session_validity="access token valid for the trading day",
        rate_limit_notes="documented per-endpoint limits; verify current numbers "
        "in Groww API docs before live use",
        # These describe OUR adapter, not Groww's API. Groww documents all of
        # these; this adapter implements none of them -- place_order,
        # modify_order, cancel_order, get_instruments and subscribe_ticks
        # every one raises FeatureNotSupportedError immediately. They were
        # declared True, which is the most misleading thing a capability
        # matrix can do: anything gating on these flags would offer Groww as
        # a tradable broker that throws on every order.
        place_order=False,
        modify_order=False,
        cancel_order=False,
        holdings=True,
        positions=True,
        funds=True,
        instruments_dump=False,
        websocket_ticks=False,
        order_postbacks=False,
        amo_orders=False,
        bracket_gtt=False,
        exchanges=["NSE", "BSE", "NFO"],
        notes="Reads only: funds, holdings and positions are implemented and "
        "unverified against a real account. Trading, instruments and streaming "
        "are not implemented at all and refuse when called. Endpoint paths and "
        "payloads MUST be verified against current Groww docs before any of "
        "this is trusted — their API surface is newer and shifts.",
    ),
    Broker.ICICI_BREEZE: BrokerCapabilities(
        broker=Broker.ICICI_BREEZE,
        adapter_status=AdapterStatus.WORKING,
        display_name="ICICI Direct Breeze",
        auth_model="api_key login -> apisession token via redirect; each request "
        "signed with SHA-256 checksum of (timestamp + body + secret)",
        session_validity="session token valid ~24h",
        rate_limit_notes="100 calls/min and 5000/day per user, plus a separate "
        "cap of 10 orders/second covering placement, modification, "
        "cancellation and square-off (SEBI algo framework). All three are "
        "enforced client-side; exceeding them is documented as blocking the "
        "account rather than returning a retryable error.",
        place_order=True,
        modify_order=True,
        cancel_order=True,
        holdings=True,
        positions=True,
        funds=True,
        instruments_dump=True,
        websocket_ticks=True,
        order_postbacks=False,
        amo_orders=False,
        bracket_gtt=False,
        # BSE removed: ICICI's own API documentation states "securities listed
        # on BSE and MCX are not available on Breeze API". Advertising it
        # offered users a segment every order would have been rejected on.
        exchanges=["NSE", "NFO"],
        notes="READ AND ORDER PATHS VERIFIED against a real account. Profile, "
        "funds, holdings and positions were exercised and corrected against "
        "what ICICI actually returns. On 2026-09-17 a cash equity order was "
        "placed and cancelled from the registered static IP during market "
        "hours — RELIND, 1 share, LIMIT well below market, CNC — which is "
        "what moved adapter_status from scaffold to working. That covers "
        "CASH EQUITY ONLY: F&O, and any product other than CNC, remain "
        "unexercised. The tick "
        "stream is likewise unverified against a live socket; it must be "
        "checked during market hours, since outside them a broken feed and a "
        "quiet market are indistinguishable. Futures and options can be "
        "built (expiry/right/strike, per the documented POST /order) but are "
        "untested; mtf and btst are unmapped and MIS is refused. BSE and MCX "
        "are absent because ICICI documents them as unavailable on Breeze. "
        "Breeze uses its own stock codes rather than NSE trading symbols "
        "(RELIANCE is RELIND), and this platform stores each broker's native "
        "codes: a strategy written against NSE symbols will not resolve on a "
        "Breeze account, and one written for Breeze will not resolve "
        "elsewhere.",
    ),
}


def get_capabilities(broker: Broker) -> BrokerCapabilities:
    return CAPABILITY_MATRIX[broker]
