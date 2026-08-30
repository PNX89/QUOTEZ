"""Tests for quotez.mt5source, driven by a fake extension module.

Absence is forced with monkeypatch rather than assumed from the runner, so these stay real
assertions on a Windows box that actually has MetaTrader5 installed. A test that only
passes because a package is missing tests the runner, not the code.

The fake module is the only way any of this field mapping gets exercised at all. No
continuous integration runner has a terminal or a broker account, and there is no
non-Windows wheel, so the mapping below is the least covered code in the repository and
the README says so.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import quotez
from quotez.errors import SourceUnavailable, SymbolNotFound
from quotez.mt5source import ORDER_TYPE_NAMES, Mt5Source
from quotez.protocol import MarketDataSource

# 2026-06-01T08:00:00Z. Chosen because it is not midnight in any common local zone, so a
# missing tzinfo shows up as a different hour rather than a different day.
EPOCH_SECONDS = 1780300800

SPEC = SimpleNamespace(
    name="EURUSD",
    description="Euro vs US Dollar",
    digits=5,
    point=1e-05,
    spread=12,
    spread_float=True,
    trade_stops_level=10,
    trade_freeze_level=0,
    trade_tick_value=1.0,
    trade_tick_size=1e-05,
    trade_contract_size=100000.0,
    volume_min=0.01,
    volume_max=100.0,
    volume_step=0.01,
    currency_base="EUR",
    currency_profit="USD",
    currency_margin="EUR",
)

RATE_ROW = {
    "time": EPOCH_SECONDS,
    "open": 1.1,
    "high": 1.102,
    "low": 1.099,
    "close": 1.1015,
    "tick_volume": 240,
    "spread": 12,
    "real_volume": 0,
}

# The bar the terminal is still building, one period on from RATE_ROW. Its tick_volume is
# deliberately tiny, which is what a half formed candle looks like and what makes it
# identifiable in an assertion.
FORMING_ROW = {**RATE_ROW, "time": EPOCH_SECONDS + 3600, "tick_volume": 3, "close": 1.1001}

# One open SHORT and one pending SELL LIMIT. Both carry MetaTrader's non-zero codes on purpose:
# type 0 is a buy for both families, so a mapping that reported everything long, or everything
# as the first name in its table, would look perfectly right against a default row. The fake
# used to hand back empty tuples for both, which left every line of both mappings unexecuted.
POSITION_ROW = SimpleNamespace(
    ticket=778899,
    symbol="EURUSD",
    type=1,
    volume=0.5,
    price_open=1.09500,
    price_current=1.10150,
    sl=1.11000,
    tp=1.08000,
    profit=-32.5,
    time=EPOCH_SECONDS,
)

ORDER_ROW = SimpleNamespace(
    ticket=112233,
    symbol="EURUSD",
    type=3,
    volume_initial=1.0,
    volume_current=0.75,
    price_open=1.12000,
    sl=1.12500,
    tp=1.10000,
    time_setup=EPOCH_SECONDS + 60,
)


class FakeTerminal(ModuleType):
    """Just enough MetaTrader5 to exercise the mapping, with the same return conventions.

    The conventions are the point: `initialize` returns a bool rather than raising, and
    `symbol_info` returns None for an unknown symbol rather than raising.
    """

    def __init__(
        self,
        *,
        initialized: bool = True,
        known: bool = True,
        positions: tuple[Any, ...] | None = (POSITION_ROW,),
        orders: tuple[Any, ...] | None = (ORDER_ROW,),
    ) -> None:
        super().__init__("MetaTrader5")
        self._initialized = initialized
        self._known = known
        self._positions = positions
        self._orders = orders
        self.shutdown_calls = 0
        self.TIMEFRAME_M1 = 1
        self.TIMEFRAME_M5 = 5
        self.TIMEFRAME_M15 = 15
        self.TIMEFRAME_M30 = 30
        self.TIMEFRAME_H1 = 16385
        self.TIMEFRAME_H4 = 16388
        self.TIMEFRAME_D1 = 16408

    def initialize(self, **_kwargs: Any) -> bool:
        return self._initialized

    def last_error(self) -> tuple[int, str]:
        return (-10005, "IPC timeout")

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def symbol_info(self, symbol: str) -> Any:
        return SPEC if self._known else None

    def symbol_info_tick(self, symbol: str) -> Any:
        return SimpleNamespace(time=EPOCH_SECONDS, bid=1.10000, ask=1.10012)

    def symbols_get(self, group: str | None = None) -> Any:
        return (SPEC,)

    def copy_rates_from_pos(self, symbol: str, period: int, start: int, count: int) -> Any:
        self.last_from_pos = (symbol, period, start, count)
        # Position 0 is the bar the terminal is still building, so the fake behaves like the
        # real thing: asking from 0 hands back a partial candle as the newest element.
        series = [FORMING_ROW, RATE_ROW]
        return series[start : start + count]

    def copy_rates_range(self, symbol: str, period: int, start: Any, end: Any) -> Any:
        self.last_range = (start, end)
        return [RATE_ROW]

    def account_info(self) -> Any:
        return SimpleNamespace(
            login=1234567890,
            currency="EUR",
            balance=5000.0,
            equity=5120.5,
            margin=200.0,
            margin_free=4920.5,
            margin_level=2560.25,
            leverage=30,
            name="A Real Person",
            server="Broker-Live",
            company="Some Broker Ltd",
        )

    def positions_get(self) -> Any:
        return self._positions

    def orders_get(self) -> Any:
        return self._orders


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> FakeTerminal:
    fake = FakeTerminal()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    return fake


@pytest.fixture
def connected_source(terminal: FakeTerminal) -> Mt5Source:
    source = Mt5Source()
    source.connect()
    return source


# --------------------------------------------------------------------------------------
# The lazy import


def test_importing_quotez_does_not_import_metatrader5() -> None:
    assert quotez  # the package is imported by the time this runs
    assert "MetaTrader5" not in sys.modules


def test_a_missing_package_is_source_unavailable_not_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A None entry in sys.modules is how absence is forced on a machine that has the wheel.
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)
    with pytest.raises(SourceUnavailable) as caught:
        Mt5Source().connect()
    message = str(caught.value)
    assert "Windows only" in message
    assert "quotez[mt5]" in message


def test_a_query_before_connect_is_source_unavailable(terminal: FakeTerminal) -> None:
    with pytest.raises(SourceUnavailable, match="not connected"):
        Mt5Source().get_quote("EURUSD")


def test_initialize_returning_false_raises_with_the_last_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # initialize() returns a bool rather than raising, so an unchecked call would leave the
    # source looking connected and fail much later.
    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeTerminal(initialized=False))
    with pytest.raises(SourceUnavailable) as caught:
        Mt5Source().connect()
    assert caught.value.code == -10005
    assert "IPC timeout" in str(caught.value)
    assert "-10005" in str(caught.value)


def test_close_shuts_the_terminal_down_once(
    connected_source: Mt5Source, terminal: FakeTerminal
) -> None:
    connected_source.close()
    connected_source.close()
    assert terminal.shutdown_calls == 1


def test_close_after_a_failed_connect_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTerminal(initialized=False)
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    source = Mt5Source()
    with pytest.raises(SourceUnavailable):
        source.connect()
    source.close()
    assert fake.shutdown_calls == 0


# --------------------------------------------------------------------------------------
# Field mapping


def test_the_source_satisfies_the_protocol() -> None:
    assert isinstance(Mt5Source(), MarketDataSource)
    assert Mt5Source().name == "mt5"
    assert Mt5Source().synthetic is False


def test_epoch_seconds_become_utc_aware_datetimes(connected_source: Mt5Source) -> None:
    # The terminal stores times in UTC with no shift, and a datetime built without a tzinfo
    # would be read as local time. This is the assertion that catches that.
    series = connected_source.get_bars("EURUSD", "H1", 1)
    bar = series.bars[0]
    assert bar.time == datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    assert bar.time.tzinfo is UTC
    assert connected_source.get_quote("EURUSD").time == bar.time
    assert (bar.open, bar.high, bar.low, bar.close) == (1.1, 1.102, 1.099, 1.1015)
    assert bar.tick_volume == 240
    # Set on an H1 bar, unlike the replay source. See the spread test below for why.
    assert bar.spread == 12
    assert series.source == "mt5"
    assert series.synthetic is False


def test_get_bars_never_returns_the_bar_the_terminal_is_still_building(
    connected_source: Mt5Source, terminal: FakeTerminal
) -> None:
    """The get_bars tool tells the model its newest bar is closed. This is why that is true.

    `copy_rates_from_pos` counts backwards from the present and position 0 is the bar being
    built right now, so the obvious call returns a partial candle whose high, low, close and
    volume are all provisional. A model reads it as a finished bar and restates it.
    """
    series = connected_source.get_bars("EURUSD", "H1", 1)
    _, _, start, _ = terminal.last_from_pos
    assert start == 1
    assert [bar.time for bar in series.bars] == [datetime(2026, 6, 1, 8, 0, tzinfo=UTC)]
    assert all(bar.tick_volume != FORMING_ROW["tick_volume"] for bar in series.bars)


def test_the_terminal_reports_a_spread_on_every_timeframe(connected_source: Mt5Source) -> None:
    # The mirror of the replay source, which clears `spread` on anything it rolled up. This
    # source rolls nothing up, so the terminal's own value is passed through rather than
    # discarded, and Bar.spread's description has to cover both.
    for timeframe in ("M1", "H1", "D1"):
        assert connected_source.get_bars("EURUSD", timeframe, 1).bars[0].spread == 12


def test_a_range_query_passes_aware_datetimes_through(
    connected_source: Mt5Source, terminal: FakeTerminal
) -> None:
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 6, 2, tzinfo=UTC)
    connected_source.get_bars_range("EURUSD", "M5", start, end)
    passed_start, passed_end = terminal.last_range
    assert passed_start.tzinfo is UTC
    assert passed_end.tzinfo is UTC
    assert (passed_start, passed_end) == (start, end)


def test_symbol_info_returning_none_maps_to_symbol_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # It returns None rather than raising, so an unchecked call turns a typo into an
    # AttributeError several frames away from the cause.
    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeTerminal(known=False))
    source = Mt5Source()
    source.connect()
    for call in (
        lambda: source.get_quote("NOPE"),
        lambda: source.symbol_info("NOPE"),
        lambda: source.get_bars("NOPE", "H1", 1),
    ):
        with pytest.raises(SymbolNotFound, match="NOPE"):
            call()


def test_the_spread_is_computed_from_the_tick(connected_source: Mt5Source) -> None:
    quote = connected_source.get_quote("EURUSD")
    assert quote.bid == 1.10000
    assert quote.ask == 1.10012
    assert quote.spread_points == 12


def test_the_account_login_is_masked_and_identity_is_dropped(
    connected_source: Mt5Source,
) -> None:
    account = connected_source.get_account()
    assert account.login_masked == "****7890"
    assert "1234567890" not in account.model_dump_json()
    # The broker and the account holder are never returned at all.
    payload = account.model_dump_json()
    for identifying in ("A Real Person", "Broker-Live", "Some Broker Ltd"):
        assert identifying not in payload
    assert account.synthetic is False
    assert account.equity == 5120.5


def test_symbol_info_maps_every_declared_field(connected_source: Mt5Source) -> None:
    spec = connected_source.symbol_info("EURUSD")
    for field in ("digits", "point", "spread", "trade_tick_value", "currency_margin"):
        assert getattr(spec, field) == getattr(SPEC, field)
    assert spec.source == "mt5"
    assert spec.synthetic is False


def test_the_group_filter_is_handed_to_the_terminal(
    connected_source: Mt5Source, monkeypatch: pytest.MonkeyPatch, terminal: FakeTerminal
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(terminal, "symbols_get", lambda group=None: seen.append(group) or (SPEC,))
    connected_source.list_symbols("*, !*USD*")
    connected_source.list_symbols()
    # Passed through rather than reimplemented, so the syntax is whatever the terminal
    # supports rather than an approximation of it.
    assert seen == ["*, !*USD*", None]


def test_an_unknown_timeframe_never_reaches_the_terminal(connected_source: Mt5Source) -> None:
    from quotez.errors import InvalidRequest

    with pytest.raises(InvalidRequest, match="W1"):
        connected_source.get_bars("EURUSD", "W1", 1)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# Positions and orders
#
# Two of the eight tools, and until the fake started returning rows every line of both
# mappings was dead: the direction lookup, the order type name, the unknown-type fallback and
# the UTC timestamp for each. Remapping POSITION_TYPE_NAMES so that both codes meant "buy" and
# emptying ORDER_TYPE_NAMES altogether left the whole suite green, which is a short position
# reported to a model as a long.


def test_a_short_position_is_reported_short_and_in_utc(connected_source: Mt5Source) -> None:
    listed = connected_source.list_positions()
    assert listed.count == 1
    held = listed.positions[0]
    # Type 1 is MetaTrader's code for a short. Nothing else in this file distinguishes the two.
    assert held.type == "sell"
    assert held.ticket == 778899
    assert held.symbol == "EURUSD"
    assert (held.volume, held.price_open, held.price_current) == (0.5, 1.09500, 1.10150)
    assert (held.sl, held.tp, held.profit) == (1.11000, 1.08000, -32.5)
    # The same rule the bars get, and for the same reason: a naive datetime would be read
    # against the local zone and the position would be timestamped hours out.
    assert held.time == datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    assert held.time.tzinfo is UTC
    assert (listed.source, listed.synthetic) == ("mt5", False)


def test_a_pending_order_keeps_its_type_name_and_both_volumes(
    connected_source: Mt5Source,
) -> None:
    listed = connected_source.list_orders()
    assert listed.count == 1
    pending = listed.orders[0]
    # Type 3 is sell_limit. A table read off by index, or one that lost an entry, lands on a
    # different word here and the model is told the wrong side of the book.
    assert pending.type == "sell_limit"
    assert pending.ticket == 112233
    assert pending.symbol == "EURUSD"
    # Initial and current are separate fields: a partially filled order differs in the second.
    assert (pending.volume_initial, pending.volume_current) == (1.0, 0.75)
    assert (pending.price_open, pending.sl, pending.tp) == (1.12000, 1.12500, 1.10000)
    assert pending.time_setup == datetime(2026, 6, 1, 8, 1, tzinfo=UTC)
    assert pending.time_setup.tzinfo is UTC
    assert (listed.source, listed.synthetic) == ("mt5", False)


def test_every_order_type_code_maps_to_the_name_metatrader_gives_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pinned by code AND by size. Reading the table out of the module would cover one code
    # fewer each time an entry was deleted and stay green while doing it.
    expected = {
        0: "buy",
        1: "sell",
        2: "buy_limit",
        3: "sell_limit",
        4: "buy_stop",
        5: "sell_stop",
        6: "buy_stop_limit",
        7: "sell_stop_limit",
        8: "close_by",
    }
    assert len(ORDER_TYPE_NAMES) == len(expected) == 9
    rows = tuple(
        SimpleNamespace(**{**vars(ORDER_ROW), "ticket": 900 + code, "type": code})
        for code in expected
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeTerminal(orders=rows))
    source = Mt5Source()
    source.connect()
    assert {order.ticket - 900: order.type for order in source.list_orders().orders} == expected


def test_an_order_type_the_terminal_invents_is_reported_rather_than_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A code outside the table is surfaced with its number attached, so a broker or a terminal
    # build that adds one shows up as an unknown rather than as whichever name happened to be
    # first. Reporting it beats guessing; guessing is how a stop becomes a limit.
    unknown = SimpleNamespace(**{**vars(ORDER_ROW), "type": 42})
    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeTerminal(orders=(unknown,)))
    source = Mt5Source()
    source.connect()
    assert source.list_orders().orders[0].type == "unknown_42"


def test_a_position_that_is_neither_long_nor_short_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A position has exactly two directions, so an unrecognised code is not a value to pass
    # through under a plausible name. It becomes a protocol error the model cannot retry into.
    impossible = SimpleNamespace(**{**vars(POSITION_ROW), "type": 7})
    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeTerminal(positions=(impossible,)))
    source = Mt5Source()
    source.connect()
    with pytest.raises(SourceUnavailable, match="position type 7"):
        source.list_positions()


@pytest.mark.parametrize("empty", ["positions", "orders"])
def test_a_none_result_is_a_source_failure_and_not_an_empty_list(
    monkeypatch: pytest.MonkeyPatch, empty: str
) -> None:
    # Both getters return None on failure rather than raising, the same convention symbol_info
    # follows. Letting that fall through as an empty list would tell a model the account holds
    # nothing open, which is the most dangerous wrong answer either of these two tools can give.
    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeTerminal(**{empty: None}))
    source = Mt5Source()
    source.connect()
    with pytest.raises(SourceUnavailable, match="IPC timeout"):
        getattr(source, f"list_{empty}")()
