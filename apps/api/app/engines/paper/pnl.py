"""Pure position/P&L math — kept free of I/O so it is trivially testable."""

from decimal import ROUND_HALF_UP, Decimal

# Position.average_price is Numeric(18, 4). A reweighted average is a division
# and carries full Decimal context precision -- 3 @ 100.00 plus 4 @ 101.00
# gives 100.5714285714285714285714286, which Postgres silently truncates to
# 100.5714 on write. That truncated value is then read back as the cost basis
# for the next fill, so the residue compounds across a position's life and
# lands in realized P&L when it closes. Rounding deliberately here, at the
# same precision the column stores, keeps the value the engine computes and
# the value the database holds identical. engine.py does the same for
# average_fill_price; the position path was the one that missed it.
_PRICE = Decimal("0.0001")


def apply_fill(
    quantity: int,
    average_price: Decimal,
    realized_pnl: Decimal,
    fill_quantity: int,
    fill_price: Decimal,
) -> tuple[int, Decimal, Decimal]:
    """Apply a signed fill (+buy/-sell) to a signed position.

    Returns (new_quantity, new_average_price, new_realized_pnl).
    Opening/adding reweights the average; reducing books realized P&L;
    crossing through zero opens the residual at the fill price.
    """
    if fill_quantity == 0:
        return quantity, average_price, realized_pnl

    if quantity == 0 or (quantity > 0) == (fill_quantity > 0):
        new_quantity = quantity + fill_quantity
        total_cost = average_price * abs(quantity) + fill_price * abs(fill_quantity)
        new_average = (total_cost / abs(new_quantity)).quantize(
            _PRICE, rounding=ROUND_HALF_UP
        )
        return new_quantity, new_average, realized_pnl

    closing = min(abs(quantity), abs(fill_quantity))
    per_unit = (fill_price - average_price) if quantity > 0 else (average_price - fill_price)
    realized_pnl += per_unit * closing

    new_quantity = quantity + fill_quantity
    if new_quantity == 0:
        return 0, Decimal("0"), realized_pnl
    if (new_quantity > 0) == (quantity > 0):
        return new_quantity, average_price, realized_pnl  # partial close
    return new_quantity, fill_price, realized_pnl  # flipped through zero


def unrealized_pnl(quantity: int, average_price: Decimal, last_price: Decimal) -> Decimal:
    if quantity == 0:
        return Decimal("0")
    return (last_price - average_price) * quantity
