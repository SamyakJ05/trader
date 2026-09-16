"""Turning an AI proposal into an order.

One function, used by both routes to an order: a human clicking approve, and
auto-execution on an account that has opted in. Shared rather than duplicated
because the interesting part is the translation -- a proposal carries a side,
a product and possibly a contract, and expressing that at a particular broker
is where the mistakes live. Two copies of that would drift, and the copy that
drifted would be the unattended one, where nobody is watching the result.

The only difference between the two paths is who decided, which is recorded:
APPROVED with auto_executed False, or AUTO_EXECUTED with it True. The risk
engine, the gates and the order pipeline are identical for both -- autonomy
removes the human check, not the machine ones.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import FeatureNotSupportedError
from app.core.redis import get_redis
from app.db.models import AIProposal, BrokerAccount, Order
from app.domain.enums import (
    Exchange,
    OptionRight,
    OrderSide,
    OrderType,
    ProductType,
    SignalType,
)
from app.engines.strategy import execution
from app.engines.strategy.base import Signal
from app.services import orders as order_service
from app.services import quotes


class ProposalNotExecutable(Exception):
    """The proposal cannot be expressed as an order at this broker."""


def client_order_id(proposal_id: uuid.UUID) -> str:
    """Derived from the proposal, so the same proposal cannot become two
    orders. Two concurrent approvals, or an approval racing an auto-execute,
    collide on the order pipeline's idempotency key and the second returns
    the first one's order rather than placing another."""
    return f"ai-{proposal_id.hex[:18]}"


async def place_from_proposal(
    db: AsyncSession,
    proposal: AIProposal,
    account: BrokerAccount,
    *,
    auto_executed: bool,
) -> Order:
    """Build the broker-appropriate order for this proposal and place it.

    Raises ProposalNotExecutable when the proposal cannot be expressed at this
    broker -- said plainly here rather than left to fail later as an opaque
    broker rejection, after the trade was already agreed to.
    """
    signal = Signal(
        symbol=proposal.symbol,
        signal_type=(
            SignalType.ENTRY_LONG
            if proposal.side == OrderSide.BUY.value
            else SignalType.EXIT_LONG
        ),
        quantity=proposal.quantity,
        order_type=OrderType(proposal.order_type),
        product=ProductType(proposal.product),
        limit_price=proposal.limit_price,
        # The contract, for a derivatives proposal. Dropping these would place
        # a cash order in the underlying rather than the option the analyst
        # described.
        expiry=proposal.expiry,
        strike=proposal.strike,
        right=OptionRight(proposal.option_right) if proposal.option_right else None,
    )
    exchange = Exchange(proposal.exchange)
    try:
        last_price = await quotes.reference_price(
            db,
            get_redis(),
            account=account,
            symbol=proposal.symbol,
            exchange=exchange.value,
        )
    except quotes.NoQuoteAvailable:
        # Only needed when a market order has to become a limit;
        # build_order_request refuses rather than guessing if it is required.
        last_price = None

    try:
        request = execution.build_order_request(
            signal=signal,
            side=OrderSide(proposal.side),
            exchange=exchange,
            broker=account.broker,
            params={},
            last_price=last_price,
        )
    except FeatureNotSupportedError as exc:
        raise ProposalNotExecutable(str(exc)) from exc

    return await order_service.place_order(
        db,
        get_redis(),
        user_id=proposal.user_id,
        account=account,
        request=request,
        client_order_id=client_order_id(proposal.id),
        auto_executed=auto_executed,
    )
