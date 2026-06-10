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
