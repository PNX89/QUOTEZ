"""A `MarketDataSource` backed by a live MetaTrader 5 terminal.

`import MetaTrader5` happens inside `_mt5()`, never at module import. The package ships
win_amd64 wheels only and has no source distribution, so a module level import would make
`import quotez` fail on macOS and Linux, where the entire test suite and the replay source
are expected to work.

Only read calls are used: symbols_get, symbol_info, symbol_info_tick, account_info,
positions_get, orders_get, copy_rates_from_pos, copy_rates_range, initialize, shutdown,
last_error. `symbol_select` is the one mutating call in that family and it is never made,
so a query cannot change the terminal's MarketWatch as a side effect. Neither `order_send`
nor `order_check` appears anywhere in this package.

This source does NOT roll bars up. It asks the terminal for the timeframe directly, because
the terminal already holds every period and re-deriving them from M1 here would be slower
and would disagree with the charts the operator has open. Two visible consequences, both in
the README's Limitations: the terminal aligns D1 and H4 to the broker's server day rather
than to UTC midnight, and it reports a `spread` on every timeframe where a locally rolled
up bar reports none.

Three traps this module exists to contain.

The still forming bar: `copy_rates_from_pos` counts position 0 as the bar currently being
built, so the obvious call returns a partial candle as the newest element and every high,
low, close and volume in it is provisional. `get_bars` starts at position 1 instead, which
is what makes the tool's promise that the newest bar is complete true here as well as on
the replay source. The current price is `get_quote`'s job.

Time zones: the terminal stores bar and tick times in UTC without any shift, but a Python
`datetime` built without a tzinfo is resolved against the LOCAL zone. So every outbound
timestamp is built with `datetime.fromtimestamp(t, tz=UTC)` and every inbound one is
required to be aware. That single rule is the difference between correct data and data
silently shifted by the machine's offset.

Missing rather than raising: `symbol_info` and `symbols_get` return None on failure instead
of raising, so an unchecked call turns a bad symbol into an AttributeError several frames
later. Every call here is checked at its call site and mapped to `SymbolNotFound`.

This is the least exercised code in the repository. No continuous integration runner has a
terminal or a broker account, and no non-Windows wheel exists, so the field mapping below
is covered by fake module tests rather than by a real connection.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, Literal, cast

from quotez.errors import InvalidRequest, SourceUnavailable, SymbolNotFound
from quotez.models import (
    Account,
    Bar,
    BarSeries,
    Order,
    OrderList,
    Position,
    PositionList,
    Quote,
    SourceName,
    SymbolList,
    SymbolSpec,
    SymbolSummary,
    Timeframe,
)

__all__ = ["Mt5Source"]

MISSING_PACKAGE_MESSAGE = (
    "MetaTrader5 is not installed. It is Windows only; install with "
    "pip install 'quotez[mt5]' on Windows."
)

# MetaTrader's own order type codes, in the order the terminal defines them.
ORDER_TYPE_NAMES = {
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

POSITION_TYPE_NAMES: dict[int, Literal["buy", "sell"]] = {0: "buy", 1: "sell"}

# `copy_rates_from_pos` numbers bars from the present backwards, and position 0 is the one
# still being built. Starting at 1 is the difference between "the newest bar is complete",
# which is what the get_bars tool tells the model, and a partial candle the model will read
# as a closed one.
FIRST_COMPLETE_BAR = 1

# Every SymbolSpec field except the two QUOTEZ stamps on. The model's field names were
# copied verbatim from symbol_info(), so reading them off the model instead of repeating
# them keeps one list where two could drift apart.
SPEC_FIELDS = tuple(name for name in SymbolSpec.model_fields if name not in {"source", "synthetic"})


def _mt5() -> ModuleType:
    """The MetaTrader5 extension, imported on first use.

    A missing package is a `SourceUnavailable`, not an ImportError, because no argument the
    caller changes will make it importable and the server has to report that as a protocol
    failure rather than as a tool result the model might try to work around. A `None` entry
    in `sys.modules`, which is how the tests simulate absence on a machine that has the
    package, raises ModuleNotFoundError and is caught by the same clause.
    """
    try:
        import MetaTrader5
    except ImportError as failure:
        raise SourceUnavailable(MISSING_PACKAGE_MESSAGE) from failure
    # The extension ships no stubs and no py.typed, so it is `Any` to a type checker on
    # every platform, including the one where it is installed. The cast is where that stops.
    return cast(ModuleType, MetaTrader5)


def _position_type(code: int) -> Literal["buy", "sell"]:
    """A position is long or short and nothing else. An unknown code is reported, not guessed."""
    try:
        return POSITION_TYPE_NAMES[code]
    except KeyError:
        raise SourceUnavailable(
            f"MetaTrader 5 reported position type {code}, which is neither buy nor sell."
        ) from None


def _timeframes(mt5: ModuleType) -> dict[str, Any]:
    """Timeframe name to terminal constant, resolved lazily so nothing imports at module load."""
    return {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }


class Mt5Source:
    """Read only market data from a running MetaTrader 5 terminal."""

    def __init__(self, *, timeout_ms: int = 60_000) -> None:
        self._timeout_ms = timeout_ms
        self._connected = False
        # mcp 2.x runs sync tool handlers on anyio worker threads, so two calls can land in
        # the extension at once. The terminal's Python bridge is not documented as thread
        # safe, so calls are serialised here rather than hoped about.
        self._lock = threading.Lock()

    @property
    def name(self) -> SourceName:
        return "mt5"

    @property
    def synthetic(self) -> bool:
        return False

    def connect(self) -> None:
        """Start or attach to the terminal. Called once from the server lifespan.

        `initialize()` launches the terminal if it is not already running, and the call is
        bounded by its `timeout` argument, documented as defaulting to 60000 milliseconds.
        The docs put no figure on the launch itself, so 60 seconds is the ceiling on the
        call rather than a measured startup time, and either way it is why this never
        happens inside a tool call. It returns False rather than raising, so the result is
        checked and `last_error()` is read for the reason.
        """
        if self._connected:
            return
        mt5 = _mt5()
        if not mt5.initialize(timeout=self._timeout_ms):
            code, message = mt5.last_error()
            raise SourceUnavailable(
                f"MetaTrader 5 initialize() failed with code {code}: {message}. "
                "Check that the terminal is installed, running and logged in.",
                code=code,
            )
        self._connected = True

    def close(self) -> None:
        """Detach from the terminal. Safe to call after a failed connect."""
        if not self._connected:
            return
        self._connected = False
        _mt5().shutdown()

    def list_symbols(self, group: str | None = None) -> SymbolList:
        mt5 = self._terminal()
        with self._lock:
            # The group filter is handed to the terminal rather than reimplemented, so the
            # syntax is whatever MetaTrader itself supports.
            found = mt5.symbols_get(group=group) if group is not None else mt5.symbols_get()
        if found is None:
            raise self._failure(mt5, "symbols_get returned no result")
        summaries = [
            SymbolSummary(
                name=info.name,
                description=info.description,
                digits=info.digits,
                point=info.point,
            )
            for info in found
        ]
        return SymbolList(
            source=self.name,
            synthetic=self.synthetic,
            count=len(summaries),
            symbols=summaries,
        )

    def get_quote(self, symbol: str) -> Quote:
        mt5 = self._terminal()
        info = self._symbol_info(mt5, symbol)
        with self._lock:
            tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise SymbolNotFound(symbol)
        spread_points = round((tick.ask - tick.bid) / info.point) if info.point else 0
        return Quote(
            symbol=symbol,
            time=datetime.fromtimestamp(tick.time, tz=UTC),
            bid=tick.bid,
            ask=tick.ask,
            spread_points=spread_points,
            source=self.name,
            synthetic=self.synthetic,
        )

    def get_bars(self, symbol: str, timeframe: Timeframe, count: int) -> BarSeries:
        mt5 = self._terminal()
        self._symbol_info(mt5, symbol)
        period = self._period(mt5, timeframe)
        with self._lock:
            rates = mt5.copy_rates_from_pos(symbol, period, FIRST_COMPLETE_BAR, count)
        return self._wrap(symbol, timeframe, rates, mt5)

    def get_bars_range(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> BarSeries:
        # No position arithmetic to do here, so no forming bar to skip either, EXCEPT when
        # `end` reaches into the interval the terminal is still building. The bounds are the
        # caller's, so that bar is returned rather than silently dropped, and the tool
        # description says so.
        mt5 = self._terminal()
        self._symbol_info(mt5, symbol)
        period = self._period(mt5, timeframe)
        with self._lock:
            rates = mt5.copy_rates_range(symbol, period, start, end)
        return self._wrap(symbol, timeframe, rates, mt5)

    def symbol_info(self, symbol: str) -> SymbolSpec:
        info = self._symbol_info(self._terminal(), symbol)
        return SymbolSpec(
            **{field: getattr(info, field) for field in SPEC_FIELDS},
            source=self.name,
            synthetic=self.synthetic,
        )

    def get_account(self) -> Account:
        mt5 = self._terminal()
        with self._lock:
            info = mt5.account_info()
        if info is None:
            raise self._failure(mt5, "account_info returned no result")
        # name, server and company are read but never returned: they identify the broker
        # and the account holder and no market data question needs either.
        return Account(
            login_masked="****" + str(info.login)[-4:],
            currency=info.currency,
            balance=info.balance,
            equity=info.equity,
            margin=info.margin,
            margin_free=info.margin_free,
            margin_level=info.margin_level,
            leverage=info.leverage,
            source=self.name,
            synthetic=self.synthetic,
        )

    def list_positions(self) -> PositionList:
        mt5 = self._terminal()
        with self._lock:
            found = mt5.positions_get()
        if found is None:
            raise self._failure(mt5, "positions_get returned no result")
        positions = [
            Position(
                ticket=row.ticket,
                symbol=row.symbol,
                type=_position_type(row.type),
                volume=row.volume,
                price_open=row.price_open,
                price_current=row.price_current,
                sl=row.sl,
                tp=row.tp,
                profit=row.profit,
                time=datetime.fromtimestamp(row.time, tz=UTC),
            )
            for row in found
        ]
        return PositionList(
            source=self.name,
            synthetic=self.synthetic,
            count=len(positions),
            positions=positions,
        )

    def list_orders(self) -> OrderList:
        mt5 = self._terminal()
        with self._lock:
            found = mt5.orders_get()
        if found is None:
            raise self._failure(mt5, "orders_get returned no result")
        orders = [
            Order(
                ticket=row.ticket,
                symbol=row.symbol,
                type=ORDER_TYPE_NAMES.get(row.type, f"unknown_{row.type}"),
                volume_initial=row.volume_initial,
                volume_current=row.volume_current,
                price_open=row.price_open,
                sl=row.sl,
                tp=row.tp,
                time_setup=datetime.fromtimestamp(row.time_setup, tz=UTC),
            )
            for row in found
        ]
        return OrderList(
            source=self.name, synthetic=self.synthetic, count=len(orders), orders=orders
        )

    def _terminal(self) -> ModuleType:
        if not self._connected:
            raise SourceUnavailable(
                "MetaTrader 5 is not connected. The terminal connection is opened once when "
                "the server starts."
            )
        return _mt5()

    def _period(self, mt5: ModuleType, timeframe: Timeframe) -> Any:
        try:
            return _timeframes(mt5)[timeframe]
        except KeyError:
            supported = ", ".join(_timeframes(mt5))
            raise InvalidRequest(
                f"Unknown timeframe {timeframe!r}. Supported timeframes: {supported}."
            ) from None

    def _symbol_info(self, mt5: ModuleType, symbol: str) -> Any:
        with self._lock:
            info = mt5.symbol_info(symbol)
        # Returns None for an unknown symbol rather than raising, so the check is the
        # difference between a clean tool error and an AttributeError several frames later.
        if info is None:
            raise SymbolNotFound(symbol)
        return info

    def _wrap(self, symbol: str, timeframe: Timeframe, rates: Any, mt5: ModuleType) -> BarSeries:
        if rates is None:
            raise self._failure(mt5, f"no rates returned for {symbol!r} at {timeframe}")
        bars = [
            Bar(
                # `time` is a Unix timestamp in SECONDS and the terminal stores it in UTC
                # with no shift, so the zone is attached rather than inferred.
                time=datetime.fromtimestamp(int(row["time"]), tz=UTC),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                tick_volume=int(row["tick_volume"]),
                spread=int(row["spread"]),
            )
            for row in rates
        ]
        return BarSeries(
            symbol=symbol,
            timeframe=timeframe,
            source=self.name,
            synthetic=self.synthetic,
            count=len(bars),
            bars=bars,
        )

    def _failure(self, mt5: ModuleType, what: str) -> SourceUnavailable:
        code, message = mt5.last_error()
        return SourceUnavailable(f"MetaTrader 5 {what}: code {code}, {message}.", code=code)
