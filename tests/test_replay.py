"""Tests for quotez.replay and the bundled fixtures.

Three kinds of assertion live here. The first checks the source honours its contract. The
second checks the shipped CSVs themselves, because they are committed artefacts of a
generator that is never run at test time: if a regeneration went wrong, or a file was hand
edited, nothing else in the suite would notice.

The third takes the files away. Every one of those reads is an I/O call that can fail, and
the failure has to arrive as `SourceUnavailable` naming the file rather than as whatever
exception the standard library happened to raise. `UnicodeDecodeError` is the one worth
naming: it is a `ValueError`, so a handler written as `except OSError` looks like it covers
file trouble and does not.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from importlib import resources
from itertools import pairwise
from pathlib import Path

import pytest

from quotez import replay
from quotez.errors import InvalidRequest, SourceUnavailable, SymbolNotFound
from quotez.replay import CSV_COLUMNS, SPECS_FILE, ReplaySource
from tests.conftest import REPLAY_SYMBOLS, SYMBOL

SESSION_MINUTES = 360
TRADING_DAYS = 10
EXPECTED_M1_BARS = SESSION_MINUTES * TRADING_DAYS


@pytest.fixture
def source() -> ReplaySource:
    return ReplaySource()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A writable copy of the bundled data, with the module's caches cleared around it.

    The caches are the reason this is a fixture and not three lines in each test: `_bars`
    and `_specs` memoise a good read, so a test that broke a file after any other test had
    loaded it would pass without ever touching the code it is aiming at.
    """
    package = resources.files("quotez.data")
    for name in (SPECS_FILE, *(f"{symbol}.csv" for symbol in REPLAY_SYMBOLS)):
        (tmp_path / name).write_text(package.joinpath(name).read_text(encoding="utf-8"))
    monkeypatch.setattr(replay, "_data_dir", lambda: tmp_path)
    replay._bars.cache_clear()
    replay._specs.cache_clear()
    yield tmp_path
    replay._bars.cache_clear()
    replay._specs.cache_clear()


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
# When the data cannot be read


def test_a_csv_that_is_not_utf8_is_a_source_failure_not_a_traceback(data_dir: Path) -> None:
    # The whole point of this test. UnicodeDecodeError is a ValueError, so it slips through
    # an `except OSError` that was written to cover exactly this situation, and the caller
    # gets a decoder's error message with no idea which file it came from.
    (data_dir / f"{SYMBOL}.csv").write_bytes(b"time,open,high\n\xff\xfe not utf 8 at all\n")
    with pytest.raises(SourceUnavailable) as caught:
        ReplaySource().get_bars(SYMBOL, "M1", 1)
    message = str(caught.value)
    assert f"{SYMBOL}.csv" in message
    assert "not valid UTF-8" in message


def test_a_specifications_file_that_is_not_utf8_is_a_source_failure(data_dir: Path) -> None:
    (data_dir / SPECS_FILE).write_bytes(b"\xff\xfe[]")
    with pytest.raises(SourceUnavailable, match="not valid UTF-8"):
        ReplaySource().list_symbols()


@pytest.mark.parametrize(
    "content",
    ["not json at all", '{"not": "a list"}', "[{}]", "[1, 2, 3]"],
    ids=["garbage", "object", "unnamed", "scalars"],
)
def test_a_malformed_specifications_file_names_the_file(data_dir: Path, content: str) -> None:
    (data_dir / SPECS_FILE).write_text(content)
    with pytest.raises(SourceUnavailable) as caught:
        ReplaySource().list_symbols()
    assert SPECS_FILE in str(caught.value)


def test_a_missing_csv_names_the_file_rather_than_raising_filenotfound(data_dir: Path) -> None:
    # What a wheel built without its package data does. The symbol is in symbols.json, so
    # the lookup succeeds and the read is what fails.
    (data_dir / f"{SYMBOL}.csv").unlink()
    with pytest.raises(SourceUnavailable) as caught:
        ReplaySource().get_quote(SYMBOL)
    assert f"{SYMBOL}.csv" in str(caught.value)
    assert "reinstall" in str(caught.value)


@pytest.mark.parametrize(
    "row",
    [
        "2026-06-01T08:00:00Z,1.0,1.1,0.9,1.05,10,not-a-number",
        "2026-06-01T08:00:00Z,1.0,1.1,0.9,1.05,ten,2",
        "the first of june,1.0,1.1,0.9,1.05,10,2",
        "2026-06-01T08:00:00,1.0,1.1,0.9,1.05,10,2",
    ],
    ids=["bad-spread", "bad-volume", "bad-time", "naive-time"],
)
def test_an_unparseable_row_names_the_file(data_dir: Path, row: str) -> None:
    # The last case is pydantic's own ValidationError, which is a ValueError, so it is
    # caught by the same clause rather than escaping as a validation traceback.
    (data_dir / f"{SYMBOL}.csv").write_text(f"{','.join(CSV_COLUMNS)}\n{row}\n")
    with pytest.raises(SourceUnavailable) as caught:
        ReplaySource().get_bars(SYMBOL, "M1", 1)
    assert f"{SYMBOL}.csv" in str(caught.value)


def test_a_csv_with_shifted_columns_is_a_source_failure_not_a_bad_request(
    data_dir: Path,
) -> None:
    # It used to raise InvalidRequest, which is the channel for something the model can fix
    # by asking differently. No argument makes a mangled file readable, so this belongs with
    # the missing terminal on the protocol channel.
    (data_dir / f"{SYMBOL}.csv").write_text("open,time,high,low,close,tick_volume,spread\n")
    with pytest.raises(SourceUnavailable) as caught:
        ReplaySource().get_bars(SYMBOL, "M1", 1)
    assert not isinstance(caught.value, InvalidRequest)
    message = str(caught.value)
    assert f"{SYMBOL}.csv" in message
    # Both the columns found and the columns wanted, so the message is enough to fix it.
    assert str(list(CSV_COLUMNS)) in message
    assert "'open', 'time'" in message


def test_the_broken_read_is_retried_rather_than_cached(data_dir: Path) -> None:
    # lru_cache stores results, not exceptions, so repairing the file has to be enough.
    good = (data_dir / f"{SYMBOL}.csv").read_text()
    (data_dir / f"{SYMBOL}.csv").write_bytes(b"\xff\xfe")
    with pytest.raises(SourceUnavailable):
        ReplaySource().get_bars(SYMBOL, "M1", 1)
    (data_dir / f"{SYMBOL}.csv").write_text(good)
    assert ReplaySource().get_bars(SYMBOL, "M1", 1).count == 1


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
