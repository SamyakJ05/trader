"""Outbound IP reporting.

ICICI whitelist one primary and one secondary address for transactional order
requests under SEBI's algo framework. Orders from anywhere else are rejected,
and the rejection does not say why — it looks like a bad session or a malformed
payload, which is where suspicion naturally falls first.

So the address this host actually leaves from is logged at startup, and
compared against BREEZE_STATIC_IP when that is configured. A mismatch after a
droplet rebuild, a reserved-IP detach or a NAT change is then visible in the
first lines of the log rather than in a failed order during market hours.

This is a diagnostic, never a gate: it must not keep the api from starting. A
lookup that fails leaves live trading exactly as configured, because refusing
to boot over an unreachable third-party echo service would turn someone else's
outage into ours.
"""

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)

# Independent providers: one returning the address as bare text, in the order
# tried. Two so a single provider's outage does not hide the check entirely.
ECHO_URLS = ("https://api.ipify.org", "https://checkip.amazonaws.com")

LOOKUP_TIMEOUT_SECONDS = 5.0


async def detect_egress_ip(timeout: float = LOOKUP_TIMEOUT_SECONDS) -> str | None:
    """This host's public IPv4 as seen from outside, or None if unknown."""
    for url in ECHO_URLS:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url)
            response.raise_for_status()
            address = response.text.strip()
            if address:
                return address
        except Exception as exc:  # noqa: BLE001 - deliberately total
            # Any failure of a third-party echo service is tolerated: a DNS
            # error, a timeout, a proxy returning HTML. None of them may stop
            # the api from starting, so the catch is as broad as the promise.
            logger.debug("egress_lookup_failed", url=url, error=str(exc))
    return None


async def report_egress_ip(expected: str | None) -> str | None:
    """Log the egress address, loudly when it is not the registered one.

    `expected` is the address registered with the broker. Without one this
    still logs what was found, which is what you need when first filling in
    the registration form.
    """
    address = await detect_egress_ip()
    if address is None:
        logger.warning(
            "egress_ip_unknown",
            detail="Could not determine outbound IP; broker IP whitelisting cannot be checked",
        )
        return None

    if expected and address != expected:
        # Every transactional call will be refused in this state. Worth saying
        # so in the terms an operator can act on.
        logger.error(
            "egress_ip_mismatch",
            detected=address,
            registered=expected,
            detail=(
                "Outbound IP differs from the address registered with the broker. "
                "Order placement, modification and cancellation will be rejected "
                "until the registered IP is updated or this host regains its "
                "reserved IP. Market data and read endpoints are unaffected."
            ),
        )
    else:
        logger.info("egress_ip", detected=address, registered=expected)
    return address
