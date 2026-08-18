"""Tests for quotez.replay and the bundled fixtures.

Two kinds of assertion live here. The first checks the source honours its contract. The
second checks the shipped CSVs themselves, because they are committed artefacts of a
generator that is deliberately never run at test time: if a regeneration went wrong, or a
file was hand edited, nothing else in the suite would notice.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from importlib import resources
from itertools import pairwise

import pytest

from quotez.errors import InvalidRequest, SymbolNotFound
from quotez.replay import CSV_COLUMNS, ReplaySource
from tests.conftest import REPLAY_SYMBOLS, SYMBOL

SESSION_MINUTES = 360
TRADING_DAYS = 10
EXPECTED_M1_BARS = SESSION_MINUTES * TRADING_DAYS


@pytest.fixture
def source() -> ReplaySource:
    return ReplaySource()


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text)


# --------------------------------------------------------------------------------------
# Loading


def test_csvs_load_through_importlib_resources(source: ReplaySource) -> None:
    # Not Path(__file__).parent: that join works in a checkout and breaks under a zipped or
    # editable install, which is the first thing uvx does.
    package = resources.files("quotez.data")
    for name in REPLAY_SYMBOLS:
        assert package.joinpath(f"{name}.csv").is_file()
    assert package.joinpath("symbols.json").is_file()
    assert source.get_bars(SYMBOL, "M1", EXPECTED_M1_BARS).count == EXPECTED_M1_BARS


def test_every_bundled_symbol_is_listed(source: ReplaySource) -> None:
    listed = source.list_symbols()
    assert [entry.name for entry in listed.symbols] == list(REPLAY_SYMBOLS)
    assert listed.count == len(REPLAY_SYMBOLS)
    assert listed.source == "replay"
    assert listed.synthetic is True


def test_the_group_filter_reaches_the_source(source: ReplaySource) -> None:
    assert [entry.name for entry in source.list_symbols("*FX*").symbols] == [
        "SYNTH_FX_ALPHA",
        "SYNTH_FX_BETA",
    ]


# --------------------------------------------------------------------------------------
# Quotes


def test_quote_time_is_the_last_bars_time_and_never_the_wall_clock(source: ReplaySource) -> None:
    last = source.get_bars(SYMBOL, "M1", 1).bars[0]
    quote = source.get_quote(SYMBOL)
    assert quote.time == last.time
    # The fixtures end in June 2026. Any clock read at all would show up here.
    assert quote.time == utc("2026-06-12T13:59:00Z")
    assert quote.time != datetime.now(UTC).replace(second=0, microsecond=0)


def test_quote_is_stable_across_calls(source: ReplaySource) -> None:
    assert source.get_quote(SYMBOL) == ReplaySource().get_quote(SYMBOL)


def test_bid_and_ask_are_derived_from_close_and_spread(source: ReplaySource) -> None:
    last = source.get_bars(SYMBOL, "M1", 1).bars[0]
    spec = source.symbol_info(SYMBOL)
    quote = source.get_quote(SYMBOL)
    assert quote.bid == last.close
    assert quote.spread_points == last.spread
    assert quote.ask == round(last.close + last.spread * spec.point, spec.digits)
    assert quote.ask > quote.bid


# --------------------------------------------------------------------------------------
# Bars


def test_get_bars_returns_the_requested_count_oldest_first(source: ReplaySource) -> None:
    series = source.get_bars(SYMBOL, "H1", 5)
    assert series.count == 5
    assert len(series.bars) == 5
    times = [bar.time for bar in series.bars]
    assert times == sorted(times)
    assert all(bar.spread is None for bar in series.bars)


def test_get_bars_asks_for_more_than_exists_and_gets_what_there_is(source: ReplaySource) -> None:
    assert source.get_bars(SYMBOL, "D1", 5000).count == TRADING_DAYS - 1


def test_get_bars_rejects_a_non_positive_count(source: ReplaySource) -> None:
    # bars[-0:] is the whole series, so this guard is the difference between an error and
    # silently returning 3600 bars.
    with pytest.raises(InvalidRequest, match="at least 1"):
        source.get_bars(SYMBOL, "M1", 0)


def test_range_is_inclusive_of_start_and_exclusive_of_end(source: ReplaySource) -> None:
    start = utc("2026-06-01T09:00:00Z")
    end = utc("2026-06-01T12:00:00Z")
    series = source.get_bars_range(SYMBOL, "H1", start, end)
    assert [bar.time for bar in series.bars] == [
        start,
        utc("2026-06-01T10:00:00Z"),
        utc("2026-06-01T11:00:00Z"),
    ]
    # Consecutive ranges tile without repeating a bar.
    following = source.get_bars_range(SYMBOL, "H1", end, utc("2026-06-01T14:00:00Z"))
    assert not {bar.time for bar in series.bars} & {bar.time for bar in following.bars}


def test_range_outside_the_data_is_empty_rather_than_an_error(source: ReplaySource) -> None:
    series = source.get_bars_range(
        SYMBOL, "H1", utc("2030-01-01T00:00:00Z"), utc("2030-01-02T00:00:00Z")
    )
    assert series.count == 0
    assert series.bars == []


def test_unknown_symbol_raises_symbol_not_found(source: ReplaySource) -> None:
    window = (utc("2026-06-01T08:00:00Z"), utc("2026-06-02T08:00:00Z"))
    calls = (
        lambda: source.get_quote("NOPE"),
        lambda: source.get_bars("NOPE", "M1", 1),
        lambda: source.get_bars_range("NOPE", "M1", *window),
        lambda: source.symbol_info("NOPE"),
    )
    for call in calls:
        with pytest.raises(SymbolNotFound, match="NOPE"):
            call()


# --------------------------------------------------------------------------------------
# Specifications and account


def test_symbol_info_matches_symbols_json(source: ReplaySource) -> None:
    raw = resources.files("quotez.data").joinpath("symbols.json").read_text(encoding="utf-8")
    stored = {entry["name"]: entry for entry in json.loads(raw)}
    for name in REPLAY_SYMBOLS:
        spec = source.symbol_info(name)
        assert spec.digits == stored[name]["digits"]
        assert spec.point == stored[name]["point"]
        assert spec.trade_tick_size == stored[name]["trade_tick_size"]
        assert spec.source == "replay"
        assert spec.synthetic is True
    # `spread` is the current spread in MetaTrader, so it comes off the last bar rather
    # than out of the static file, and it agrees with the quote.
    assert "spread" not in stored[SYMBOL]
    assert source.symbol_info(SYMBOL).spread == source.get_quote(SYMBOL).spread_points


def test_account_is_labelled_synthetic(source: ReplaySource) -> None:
    account = source.get_account()
    assert account.synthetic is True
    assert account.source == "replay"
    assert account.login_masked == "****0000"
    assert account.currency == "SYN"
    assert account.balance == 100_000.00


def test_positions_and_orders_are_empty(source: ReplaySource) -> None:
    positions = source.list_positions()
    orders = source.list_orders()
    assert (positions.count, positions.positions) == (0, [])
    assert (orders.count, orders.orders) == (0, [])
    assert positions.synthetic is True
    assert orders.synthetic is True


# --------------------------------------------------------------------------------------
# The committed fixtures themselves


@pytest.mark.parametrize("symbol", REPLAY_SYMBOLS)
def test_bundled_file_has_the_expected_shape(symbol: str) -> None:
    text = resources.files("quotez.data").joinpath(f"{symbol}.csv").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == ",".join(CSV_COLUMNS)
    assert len(lines) - 1 == EXPECTED_M1_BARS


@pytest.mark.parametrize("symbol", REPLAY_SYMBOLS)
def test_bundled_bars_are_ohlc_consistent_and_gapped(symbol: str) -> None:
    bars = ReplaySource().get_bars(symbol, "M1", EXPECTED_M1_BARS).bars
    for bar in bars:
        assert bar.low <= min(bar.open, bar.close)
        assert bar.high >= max(bar.open, bar.close)
        assert bar.tick_volume > 0
        assert bar.spread is not None and bar.spread > 0
        assert bar.time.tzinfo is UTC
        assert 8 <= bar.time.hour < 14
        assert bar.time.weekday() < 5

    # 9 overnight breaks and 1 weekend. Aggregation only gets interesting over holes, so a
    # regeneration that produced a continuous series would silently weaken the roll-up
    # tests rather than fail them.
    breaks = [
        (later.time - earlier.time)
        for earlier, later in pairwise(bars)
        if later.time - earlier.time != timedelta(minutes=1)
    ]
    # Bars carry their OPEN time, so the last one of a session opens at 13:59 and the break
    # to the next 08:00 open is 18h01m rather than 18h.
    overnight = timedelta(hours=18, minutes=1)
    assert len(breaks) == TRADING_DAYS - 1
    assert breaks.count(overnight) == 8
    assert breaks.count(overnight + timedelta(days=2)) == 1
