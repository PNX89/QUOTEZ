"""Tests for quotez.aggregate.

The roll-up is the one piece of arithmetic in the server that fails silently: a wrong
bucket boundary or a summed spread produces bars that look entirely reasonable. So the
gaps are built explicitly here (an overnight break and a weekend) and the bars are pinned
value by value rather than checked in aggregate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from quotez.aggregate import (
    TIMEFRAME_SECONDS,
    aggregate,
    aggregate_seconds,
    bucket_start,
    timeframe_seconds,
)
from quotez.errors import InvalidRequest
from quotez.models import Bar

SESSION_MINUTES = 360  # 08:00 to 14:00 UTC, the shape the bundled replay data has


def minute_bars(start: str, count: int, *, price: float = 100.0) -> list[Bar]:
    """`count` consecutive M1 bars, each one point above the last.

    Bar i has open price+i, high open+2, low open-3, close open+1 and tick_volume 10+i,
    so every aggregate of a known slice can be worked out by hand.
    """
    first = datetime.fromisoformat(start)
    return [
        Bar(
            time=first + timedelta(minutes=i),
            open=price + i,
            high=price + i + 2,
            low=price + i - 3,
            close=price + i + 1,
            tick_volume=10 + i,
            spread=2,
        )
        for i in range(count)
    ]


def session(day: str, *, price: float = 100.0) -> list[Bar]:
    """One 08:00 to 14:00 UTC session of M1 bars."""
    return minute_bars(f"{day}T08:00:00Z", SESSION_MINUTES, price=price)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text)


# --------------------------------------------------------------------------------------
# Bucketing


@pytest.mark.parametrize(
    ("moment", "seconds", "expected"),
    [
        ("2026-06-01T08:00:00Z", 3600, "2026-06-01T08:00:00Z"),
        ("2026-06-01T08:59:59Z", 3600, "2026-06-01T08:00:00Z"),
        ("2026-06-01T13:59:00Z", 14400, "2026-06-01T12:00:00Z"),
        ("2026-06-01T23:59:59Z", 86400, "2026-06-01T00:00:00Z"),
        # Epoch zero and before it: floor division must round down, not toward zero.
        ("1970-01-01T00:00:00Z", 86400, "1970-01-01T00:00:00Z"),
        ("1969-12-31T23:59:00Z", 3600, "1969-12-31T23:00:00Z"),
    ],
)
def test_bucket_start_floors_on_the_epoch_second(moment: str, seconds: int, expected: str) -> None:
    edge = bucket_start(utc(moment), seconds)
    assert edge == utc(expected)
    assert edge.timestamp() % seconds == 0


def test_bucket_start_labels_the_left_edge_in_utc() -> None:
    # 15:30 in a +02:00 zone is 13:30 UTC, so the H4 bucket is the 12:00 UTC one and the
    # label carries UTC, not the caller's offset.
    local = datetime(2026, 6, 1, 15, 30, tzinfo=timezone(timedelta(hours=2)))
    edge = bucket_start(local, TIMEFRAME_SECONDS["H4"])
    assert edge == utc("2026-06-01T12:00:00Z")
    assert edge.tzinfo is UTC


def test_bucket_start_rejects_a_naive_timestamp() -> None:
    with pytest.raises(InvalidRequest, match="no timezone"):
        bucket_start(datetime(2026, 6, 1, 8, 0), 3600)


# --------------------------------------------------------------------------------------
# Composition


def test_m1_passthrough_is_identity() -> None:
    bars = minute_bars("2026-06-01T08:00:00Z", 90)
    assert aggregate(bars, "M1") == bars


def test_ohlc_is_first_open_max_high_min_low_last_close() -> None:
    bars = minute_bars("2026-06-01T08:00:00Z", 90, price=100.0)
    hour = aggregate(bars, "H1")[0]
    inputs = bars[:60]
    assert hour.open == inputs[0].open
    assert hour.high == max(candle.high for candle in inputs)
    assert hour.low == min(candle.low for candle in inputs)
    assert hour.close == inputs[-1].close
    # Pinned by hand as well, so a change in the helper cannot make the test agree with a
    # broken implementation.
    assert (hour.open, hour.high, hour.low, hour.close) == (100.0, 161.0, 97.0, 160.0)


def test_tick_volume_is_summed() -> None:
    bars = minute_bars("2026-06-01T08:00:00Z", 90)
    hour = aggregate(bars, "H1")[0]
    assert hour.tick_volume == sum(candle.tick_volume for candle in bars[:60])


def test_spread_is_dropped_rather_than_summed() -> None:
    bars = minute_bars("2026-06-01T08:00:00Z", 90)
    hour = aggregate(bars, "H1")[0]
    assert all(candle.spread == 2 for candle in bars)
    assert hour.spread is None


def test_aggregated_bars_are_labelled_by_the_left_edge_in_utc() -> None:
    bars = minute_bars("2026-06-01T08:00:00Z", 190)
    hours = aggregate(bars, "H1")
    assert [candle.time for candle in hours] == [
        utc("2026-06-01T08:00:00Z"),
        utc("2026-06-01T09:00:00Z"),
        utc("2026-06-01T10:00:00Z"),
    ]
    assert all(candle.time.tzinfo is UTC for candle in hours)


# --------------------------------------------------------------------------------------
# Completeness and gaps


def test_incomplete_trailing_bucket_is_dropped() -> None:
    bars = minute_bars("2026-06-01T08:00:00Z", 90)
    hours = aggregate(bars, "H1")
    # The 09:00 bucket holds 30 of its 60 minutes and no bar exists at or after 10:00,
    # so it is still forming and is not emitted as a partial bar.
    assert [candle.time for candle in hours] == [utc("2026-06-01T08:00:00Z")]
    assert hours[0].tick_volume == sum(candle.tick_volume for candle in bars[:60])


def test_weekend_gap_does_not_merge_friday_into_monday() -> None:
    friday = session("2026-06-05", price=100.0)
    monday = session("2026-06-08", price=200.0)
    days = aggregate(friday + monday, "D1")

    # Monday's bucket is the newest and therefore incomplete, so exactly one day survives.
    assert len(days) == 1
    assert days[0].time == utc("2026-06-05T00:00:00Z")
    assert days[0].high == max(candle.high for candle in friday)
    assert days[0].close == friday[-1].close
    # Positional grouping would have folded both sessions into one bar and taken its high
    # from Monday, 561 rather than 461.
    assert days[0].high == 461.0


def test_session_gap_yields_a_bucket_holding_only_that_session() -> None:
    first = session("2026-06-01")
    second = session("2026-06-02")
    hours = aggregate(first + second, "H4")

    by_time = {candle.time: candle for candle in hours}
    # 12:00 to 16:00 on day one is only two thirds filled: the session closes at 14:00.
    # It is still emitted, because a later bar proves the data did not simply run out.
    partial = by_time[utc("2026-06-01T12:00:00Z")]
    assert partial.tick_volume == sum(candle.tick_volume for candle in first[240:])
    assert partial.close == first[-1].close
    assert partial.high == max(candle.high for candle in first[240:])
    # Day two's 12:00 bucket is the newest and is dropped, so only day one's survives.
    assert utc("2026-06-02T12:00:00Z") not in by_time
    assert sorted(by_time) == [
        utc("2026-06-01T08:00:00Z"),
        utc("2026-06-01T12:00:00Z"),
        utc("2026-06-02T08:00:00Z"),
    ]


# --------------------------------------------------------------------------------------
# Contract


def test_empty_input_returns_an_empty_list() -> None:
    assert aggregate([], "H1") == []
    assert aggregate_seconds([], 3600) == []


def test_unknown_timeframe_raises_invalid_request() -> None:
    with pytest.raises(InvalidRequest) as caught:
        aggregate(minute_bars("2026-06-01T08:00:00Z", 5), "W1")
    assert "W1" in str(caught.value)
    assert "M15" in str(caught.value)


def test_non_multiple_period_is_rejected() -> None:
    bars = minute_bars("2026-06-01T08:00:00Z", 5)
    with pytest.raises(InvalidRequest, match="whole number"):
        aggregate_seconds(bars, 90)
    with pytest.raises(InvalidRequest, match="whole number"):
        aggregate_seconds(bars, 0)


def test_every_supported_timeframe_is_a_whole_number_of_minutes() -> None:
    assert all(seconds % 60 == 0 for seconds in TIMEFRAME_SECONDS.values())
    assert timeframe_seconds("H4") == 4 * 3600


def test_unordered_input_is_rejected_rather_than_sorted() -> None:
    bars = minute_bars("2026-06-01T08:00:00Z", 5)
    with pytest.raises(InvalidRequest, match="ascending"):
        aggregate(list(reversed(bars)), "H1")
    with pytest.raises(InvalidRequest, match="ascending"):
        aggregate([bars[0], bars[0]], "H1")
