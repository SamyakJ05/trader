"""Pure position/P&L math — kept free of I/O so it is trivially testable."""

from decimal import Decimal


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
        new_average = total_cost / abs(new_quantity)
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
