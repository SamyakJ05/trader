from decimal import Decimal

from app.engines.paper.pnl import apply_fill, unrealized_pnl

D = Decimal


def test_open_long():
    qty, avg, realized = apply_fill(0, D("0"), D("0"), 10, D("100"))
    assert (qty, avg, realized) == (10, D("100"), D("0"))


def test_add_to_long_reweights_average():
    qty, avg, realized = apply_fill(10, D("100"), D("0"), 10, D("110"))
    assert qty == 20
    assert avg == D("105")
    assert realized == D("0")


def test_partial_close_books_realized():
    qty, avg, realized = apply_fill(10, D("100"), D("0"), -4, D("110"))
    assert qty == 6
    assert avg == D("100")
    assert realized == D("40")


def test_full_close():
    qty, avg, realized = apply_fill(10, D("100"), D("0"), -10, D("90"))
    assert (qty, avg, realized) == (0, D("0"), D("-100"))


def test_flip_through_zero():
    qty, avg, realized = apply_fill(10, D("100"), D("0"), -15, D("120"))
    assert qty == -5
    assert avg == D("120")  # residual short opens at fill price
    assert realized == D("200")


def test_short_close_profit():
    qty, avg, realized = apply_fill(-10, D("100"), D("0"), 10, D("90"))
    assert (qty, avg, realized) == (0, D("0"), D("100"))


def test_unrealized():
    assert unrealized_pnl(10, D("100"), D("105")) == D("50")
    assert unrealized_pnl(-10, D("100"), D("105")) == D("-50")
    assert unrealized_pnl(0, D("100"), D("105")) == D("0")


# ── the stored average must equal the computed one ───────────────────
# Position.average_price is Numeric(18, 4). A reweighted average is a
# division and carries full context precision, which Postgres truncates
# silently on write -- and the truncated value then becomes the cost
# basis for the next fill, so the error compounds across a position's
# life rather than staying put.


def test_a_reweighted_average_fits_the_column_it_is_stored_in():
    """3 @ 100.00 then 4 @ 101.00 divides to
    100.5714285714285714285714286 unrounded. Anything past the fourth
    decimal is lost on write, so the engine must not keep digits the
    database will discard."""
    _, average, _ = apply_fill(3, Decimal("100.00"), Decimal("0"), 4, Decimal("101.00"))
    assert average == Decimal("100.5714")
    assert -average.as_tuple().exponent <= 4, (
        f"average carries {-average.as_tuple().exponent} dp, column holds 4"
    )


def test_repeated_reweighting_does_not_drift_from_the_stored_value():
    """The compounding this prevents: feed each result back in as the next
    fill's basis, exactly as the database round-trip does."""
    quantity, average, realized = 0, Decimal("0"), Decimal("0")
    for i in range(20):
        quantity, average, realized = apply_fill(
            quantity, average, realized, 3, Decimal("100.00") + Decimal(i) / 7
        )
        assert -average.as_tuple().exponent <= 4, (
            f"fill {i} produced {average}, which the column cannot hold"
        )
