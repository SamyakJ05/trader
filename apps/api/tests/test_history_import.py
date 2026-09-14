from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.cli.import_history import normalize_frame

pd = pytest.importorskip("pandas", reason="optional history dependencies")

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def frame():
    return pd.DataFrame(
        {"Open": [100], "High": [110], "Low": [90], "Close": [105], "Volume": [1000]},
        index=pd.to_datetime(["2026-09-01"]),
    )


def test_daily_naive_index_is_nse_midnight():
    row = normalize_frame(frame(), "1d", NOW)[0]
    assert row["ts"] == datetime(2026, 8, 31, 18, 30, tzinfo=timezone.utc)
    assert row["close"] == Decimal("105.0000")
    assert row["volume"] == 1000


@pytest.mark.parametrize(
    "column,value",
    [("Open", float("nan")), ("Low", 101), ("High", 95), ("Volume", -1), ("Volume", 1.5)],
)
def test_bad_download_is_rejected_before_writing(column, value):
    data = frame().astype(float)
    data.loc[data.index[0], column] = value
    with pytest.raises(ValueError):
        normalize_frame(data, "1d", NOW)


def test_intraday_without_timezone_refused():
    with pytest.raises(ValueError, match="timezone"):
        normalize_frame(frame(), "1m", NOW)


def test_duplicates_refused():
    with pytest.raises(ValueError, match="Duplicate"):
        normalize_frame(pd.concat([frame(), frame()]), "1d", NOW)


def test_incomplete_daily_bar_not_imported():
    with pytest.raises(ValueError, match="No completed"):
        normalize_frame(frame(), "1d", datetime(2026, 9, 1, 6, tzinfo=timezone.utc))


def test_a_two_for_one_split_is_flagged():
    """Unadjusted data records a split as an overnight halving. Every
    downstream metric reads that as a crash, so the importer says so."""
    from decimal import Decimal as D

    from app.cli.import_history import detect_suspect_gaps

    rows = [
        dict(ts=datetime(2026, 3, 2, tzinfo=timezone.utc), open=D(1000), close=D(1000)),
        dict(ts=datetime(2026, 3, 3, tzinfo=timezone.utc), open=D(500), close=D(505)),
    ]
    suspects = detect_suspect_gaps(rows)
    assert len(suspects) == 1
    assert suspects[0]["likely_split"] == "2:1"


def test_an_ordinary_move_is_not_flagged():
    from decimal import Decimal as D

    from app.cli.import_history import detect_suspect_gaps

    rows = [
        dict(ts=datetime(2026, 3, 2, tzinfo=timezone.utc), open=D(1000), close=D(1000)),
        dict(ts=datetime(2026, 3, 3, tzinfo=timezone.utc), open=D(1050), close=D(1060)),
    ]
    assert detect_suspect_gaps(rows) == []


def test_a_large_move_that_is_not_a_split_ratio_is_still_flagged():
    """A genuine 45% crash is reported too — this cannot tell them apart, and
    silence would be the worse failure."""
    from decimal import Decimal as D

    from app.cli.import_history import detect_suspect_gaps

    rows = [
        dict(ts=datetime(2026, 3, 2, tzinfo=timezone.utc), open=D(1000), close=D(1000)),
        dict(ts=datetime(2026, 3, 3, tzinfo=timezone.utc), open=D(550), close=D(560)),
    ]
    suspects = detect_suspect_gaps(rows)
    assert len(suspects) == 1
    assert suspects[0]["likely_split"] is None
