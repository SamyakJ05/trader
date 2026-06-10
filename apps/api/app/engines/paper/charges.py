"""Charges placeholder.

TODO(charges): implement the real Indian cost stack — brokerage (per broker
plan), STT/CTT, exchange transaction charges, SEBI fees, stamp duty, GST.
Each broker adapter should eventually expose its own brokerage rule; the
statutory components are broker-independent.
"""

from decimal import Decimal

from app.domain.enums import OrderSide, ProductType


def estimate_charges(
    side: OrderSide,
    product: ProductType,
    quantity: int,
    price: Decimal,
) -> Decimal:
    return Decimal("0")
