"""A `MarketDataSource` backed by the CSVs bundled in `quotez.data`.

This is a real implementation of the protocol, not a mock. The tool layer above cannot tell
it apart from the MetaTrader source, so the whole test suite exercises the real code path
on a machine with no terminal installed.

It stores M1 bars and rolls everything else up through `quotez.aggregate`. The MetaTrader
source does not: a terminal already holds every period, so it is asked for the timeframe
directly. The two answers therefore differ in two visible ways, both documented in the
README's Limitations. D1 and H4 land on UTC boundaries here and on the broker's server day
there, and `spread` is cleared on a bar rolled up here while the terminal reports one on
every timeframe.

Nothing here reads a clock. `get_quote` is derived from the last stored bar, which is what
makes the README transcript reproducible and timestamp assertions stable. A wall clock read
in a replay source is the quiet way a fixture becomes flaky.

Files are read through `importlib.resources`, never `Path(__file__).parent`. The path join
works in a source checkout and breaks under a zipped or editable install, which is the
first thing `uvx` does.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from functools import cache, lru_cache
from importlib import resources
from typing import Any

from quotez.aggregate import aggregate
from quotez.errors import InvalidRequest, SymbolNotFound
from quotez.groups import match_group
from quotez.models import (
    Account,
    Bar,
    BarSeries,
    OrderList,
    PositionList,
    Quote,
    SourceName,
    SymbolList,
    SymbolSpec,
    SymbolSummary,
    Timeframe,
)

__all__ = ["ReplaySource"]

CSV_COLUMNS = ("time", "open", "high", "low", "close", "tick_volume", "spread")

# The replay account describes no real account and is shaped so that it cannot be mistaken
# for one: a login of all zeros, a currency code that no broker issues, and round figures.
# Every payload it appears in also carries synthetic=true.
REPLAY_LOGIN_MASKED = "****0000"
REPLAY_CURRENCY = "SYN"
REPLAY_BALANCE = 100_000.00
REPLAY_LEVERAGE = 100


@lru_cache(maxsize=1)
def _specs() -> dict[str, dict[str, Any]]:
    """The contract specifications, keyed by symbol, in file order."""
    raw = resources.files("quotez.data").joinpath("symbols.json").read_text(encoding="utf-8")
    return {entry["name"]: entry for entry in json.loads(raw)}


@cache
def _bars(symbol: str) -> tuple[Bar, ...]:
    """Every stored M1 bar for `symbol`, oldest first."""
    text = resources.files("quotez.data").joinpath(f"{symbol}.csv").read_text(encoding="utf-8")
    reader = csv.DictReader(text.splitlines())
    if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
        raise InvalidRequest(
            f"Replay file for {symbol!r} has columns {reader.fieldnames}, expected "
            f"{list(CSV_COLUMNS)}."
        )
    return tuple(
        Bar(
            time=datetime.fromisoformat(row["time"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            tick_volume=int(row["tick_volume"]),
            spread=int(row["spread"]),
        )
        for row in reader
    )


class ReplaySource:
    """Read only market data replayed from the bundled synthetic files."""

    @property
    def name(self) -> SourceName:
        return "replay"

    @property
    def synthetic(self) -> bool:
        return True

    def connect(self) -> None:
        """Nothing to acquire. Files are read lazily and cached on first use."""

    def close(self) -> None:
        """Nothing to release."""

    def list_symbols(self, group: str | None = None) -> SymbolList:
        summaries = [
            SymbolSummary(
                name=spec["name"],
                description=spec["description"],
                digits=spec["digits"],
                point=spec["point"],
            )
            for name, spec in _specs().items()
            if match_group(name, group)
        ]
        return SymbolList(
            source=self.name,
            synthetic=self.synthetic,
            count=len(summaries),
            symbols=summaries,
        )

    def get_quote(self, symbol: str) -> Quote:
        spec = self._spec(symbol)
        last = _bars(symbol)[-1]
        # Derived from the stored bar, never from the clock: bid is its close, ask is that
        # plus the bar's own spread in points. Rounded to the instrument's digits because a
        # float sum at 1e-05 resolution otherwise leaks a trailing 0.000000000001.
        # Every stored M1 bar carries a spread; `Bar.spread` is optional only so that an
        # aggregated bar can report None.
        spread_points = last.spread or 0
        ask = round(last.close + spread_points * spec["point"], spec["digits"])
        return Quote(
            symbol=symbol,
            time=last.time,
            bid=last.close,
            ask=ask,
            spread_points=spread_points,
            source=self.name,
            synthetic=self.synthetic,
        )

    def get_bars(self, symbol: str, timeframe: Timeframe, count: int) -> BarSeries:
        # `bars[-0:]` is the whole series rather than an empty one, so a non-positive count
        # is rejected here instead of quietly returning every stored bar.
        if count < 1:
            raise InvalidRequest(f"count must be at least 1, got {count}.")
        return self._wrap(symbol, timeframe, self._series(symbol, timeframe)[-count:])

    def get_bars_range(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> BarSeries:
        bars = [bar for bar in self._series(symbol, timeframe) if start <= bar.time < end]
        return self._wrap(symbol, timeframe, bars)

    def symbol_info(self, symbol: str) -> SymbolSpec:
        spec = self._spec(symbol)
        # `spread` is the CURRENT spread in MetaTrader, so it comes off the last stored bar
        # rather than out of symbols.json, and therefore agrees with get_quote.
        return SymbolSpec(
            **spec,
            spread=_bars(symbol)[-1].spread or 0,
            source=self.name,
            synthetic=self.synthetic,
        )

    def get_account(self) -> Account:
        return Account(
            login_masked=REPLAY_LOGIN_MASKED,
            currency=REPLAY_CURRENCY,
            balance=REPLAY_BALANCE,
            equity=REPLAY_BALANCE,
            margin=0.0,
            margin_free=REPLAY_BALANCE,
            # MetaTrader reports a margin level of 0 while no margin is in use, rather than
            # dividing by zero.
            margin_level=0.0,
            leverage=REPLAY_LEVERAGE,
            source=self.name,
            synthetic=self.synthetic,
        )

    def list_positions(self) -> PositionList:
        return PositionList(source=self.name, synthetic=self.synthetic, count=0, positions=[])

    def list_orders(self) -> OrderList:
        return OrderList(source=self.name, synthetic=self.synthetic, count=0, orders=[])

    def _spec(self, symbol: str) -> dict[str, Any]:
        try:
            return _specs()[symbol]
        except KeyError:
            raise SymbolNotFound(symbol) from None

    def _series(self, symbol: str, timeframe: Timeframe) -> list[Bar]:
        self._spec(symbol)
        return aggregate(_bars(symbol), timeframe)

    def _wrap(self, symbol: str, timeframe: Timeframe, bars: list[Bar]) -> BarSeries:
        return BarSeries(
            symbol=symbol,
            timeframe=timeframe,
            source=self.name,
            synthetic=self.synthetic,
            count=len(bars),
            bars=bars,
        )
