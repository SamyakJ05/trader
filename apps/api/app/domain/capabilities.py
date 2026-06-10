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
        holdings=False,
        positions=True,
        funds=True,
        instruments_dump=False,
        websocket_ticks=False,
        order_postbacks=False,
        amo_orders=False,
        bracket_gtt=False,
        exchanges=["NSE", "BSE", "NFO"],
        notes="Simulated fills against an internal price feed. No holdings model yet "
        "(positions only).",
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
        amo_orders=True,
        bracket_gtt=True,
        exchanges=["NSE", "BSE", "NFO", "BFO", "MCX", "CDS"],
        notes="Adapter scaffolded against documented Kite Connect v3 REST endpoints. "
        "NOT verified against a live account. WebSocket ticks not implemented.",
    ),
    Broker.GROWW: BrokerCapabilities(
        broker=Broker.GROWW,
        adapter_status=AdapterStatus.SCAFFOLD,
        display_name="Groww Trading API",
        auth_model="API key + secret (TOTP-based token generation) or "
        "direct daily access token",
        session_validity="access token valid for the trading day",
        rate_limit_notes="documented per-endpoint limits; verify current numbers "
        "in Groww API docs before live use",
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
        exchanges=["NSE", "BSE", "NFO"],
        notes="Adapter scaffolded; endpoint paths and payloads MUST be verified "
        "against current Groww docs — their API surface is newer and shifts. "
        "Feed/streaming not implemented.",
    ),
    Broker.ICICI_BREEZE: BrokerCapabilities(
        broker=Broker.ICICI_BREEZE,
        adapter_status=AdapterStatus.SCAFFOLD,
        display_name="ICICI Direct Breeze",
        auth_model="api_key login -> apisession token via redirect; each request "
        "signed with SHA-256 checksum of (timestamp + body + secret)",
        session_validity="session token valid ~24h",
        rate_limit_notes="documented limits: 100 calls/min, 5000/day per user "
        "(verify current numbers in Breeze docs)",
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
        exchanges=["NSE", "BSE", "NFO"],
        notes="Adapter scaffolded with Breeze checksum auth wiring. NOT verified "
        "against a live account. Market coverage narrower than Zerodha (no MCX "
        "in Breeze API as documented).",
    ),
}


def get_capabilities(broker: Broker) -> BrokerCapabilities:
    return CAPABILITY_MATRIX[broker]
