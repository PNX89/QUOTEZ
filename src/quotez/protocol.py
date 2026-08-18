"""The single interface a market data source implements.

Every tool in this server is written against `MarketDataSource` and none of them import
MetaTrader5. That is what makes the bundled replay source a first class implementation
rather than a mock: the tool layer cannot tell the two apart, so the whole suite runs on
macOS and Linux with no terminal installed and the tests exercise the real code path.

A source owns how bars are stored and rolled up, how the group filter is applied, and its
own connection lifecycle. It does NOT own the symbol whitelist. The server applies that on
top of whatever a source returns, so no source can widen the exposed universe by accident.

Every method is synchronous. In mcp 2.x a sync tool handler runs on a worker thread via
`anyio.to_thread.run_sync()`, which is what a blocking C extension like MetaTrader5 wants.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from quotez.models import (
    Account,
    BarSeries,
    OrderList,
    PositionList,
    Quote,
    SourceName,
    SymbolList,
    SymbolSpec,
    Timeframe,
)

__all__ = ["MarketDataSource"]


@runtime_checkable
class MarketDataSource(Protocol):
    """Read only market data. There is no method here that changes anything.

    Implementations raise `SymbolNotFound` for an unknown or unavailable symbol,
    `InvalidRequest` for arguments that are out of contract, and `SourceUnavailable` when
    the source itself cannot serve anything.
    """

    @property
    def name(self) -> SourceName:
        """Identifier stamped into every payload's `source` field."""
        ...

    @property
    def synthetic(self) -> bool:
        """True when this source's prices are generated rather than observed."""
        ...

    def connect(self) -> None:
        """Acquire whatever the source needs to serve requests. Idempotent.

        Called once from the server lifespan, never per request. `mt5.initialize()` will
        start the terminal if it is not already running, bounded by a timeout that defaults
        to 60000 milliseconds, so doing this inside a tool risks making the first call look
        hung.
        """
        ...

    def close(self) -> None:
        """Release everything `connect` acquired. Idempotent, and safe after a failed connect."""
        ...

    def list_symbols(self, group: str | None = None) -> SymbolList:
        """Instruments this source has, optionally filtered by MetaTrader group syntax."""
        ...

    def get_quote(self, symbol: str) -> Quote:
        """Latest bid and ask for one instrument."""
        ...

    def get_bars(self, symbol: str, timeframe: Timeframe, count: int) -> BarSeries:
        """The most recent `count` bars, oldest first."""
        ...

    def get_bars_range(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> BarSeries:
        """Bars whose open time falls in [start, end), oldest first.

        Both bounds are UTC aware. `start` is inclusive and `end` is exclusive, so
        consecutive ranges tile without repeating a bar.
        """
        ...

    def symbol_info(self, symbol: str) -> SymbolSpec:
        """Contract specification for one instrument."""
        ...

    def get_account(self) -> Account:
        """Account state, with the login masked and identifying fields dropped."""
        ...

    def list_positions(self) -> PositionList:
        """Open positions."""
        ...

    def list_orders(self) -> OrderList:
        """Pending orders."""
        ...
