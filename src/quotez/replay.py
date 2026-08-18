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

A file that cannot be read is `SourceUnavailable`, never a bare traceback and never an
`InvalidRequest`. It is not a bad request: no argument the model can send will make a
truncated wheel or a corrupt CSV readable, so it belongs on the protocol channel with the
missing terminal rather than coming back as a tool result that invites a retry. Note which
exceptions that means catching. `read_text` raises `OSError` when the file is missing or
unreadable and `UnicodeDecodeError` when the bytes are not UTF-8, and `UnicodeDecodeError`
is a `ValueError`: an `except OSError` alone looks thorough and lets the corrupt file
through as a traceback.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from functools import cache, lru_cache
from importlib import resources

# importlib.resources.abc, not importlib.abc: the alias in the latter was removed in 3.14.
from importlib.resources.abc import Traversable
from typing import Any

from quotez.aggregate import aggregate
from quotez.errors import InvalidRequest, SourceUnavailable, SymbolNotFound
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

SPECS_FILE = "symbols.json"

# The replay account describes no real account and is shaped so that it cannot be mistaken
# for one: a login of all zeros, a currency code that no broker issues, and round figures.
# Every payload it appears in also carries synthetic=true.
REPLAY_LOGIN_MASKED = "****0000"
REPLAY_CURRENCY = "SYN"
REPLAY_BALANCE = 100_000.00
REPLAY_LEVERAGE = 100


def _data_dir() -> Traversable:
    """The bundled data directory. A seam, so a test can point the reader at a broken file."""
    return resources.files("quotez.data")


def _read_data(filename: str) -> str:
    """Text of one bundled data file, or `SourceUnavailable` naming the file and the reason.

    Both clauses are load bearing. `OSError` is the missing or unreadable file, which is
    what a wheel built without the package data produces. `UnicodeDecodeError` is a
    `ValueError` and would sail past an `except OSError`, so a file with one stray byte in
    it would reach a caller as a raw traceback instead of as the source failure it is.
    """
    try:
        return _data_dir().joinpath(filename).read_text(encoding="utf-8")
    except OSError as failure:
        raise SourceUnavailable(
            f"Replay data file {filename!r} could not be read: {failure}. The bundled files "
            "ship inside the quotez package; reinstall it."
        ) from failure
    except UnicodeDecodeError as failure:
        raise SourceUnavailable(
            f"Replay data file {filename!r} is not valid UTF-8: {failure}."
        ) from failure


@lru_cache(maxsize=1)
def _specs() -> dict[str, dict[str, Any]]:
    """The contract specifications, keyed by symbol, in file order."""
    raw = _read_data(SPECS_FILE)
    try:
        return {entry["name"]: entry for entry in json.loads(raw)}
    except (ValueError, TypeError, KeyError) as failure:
        raise SourceUnavailable(
            f"Replay data file {SPECS_FILE!r} is not a list of named instrument "
            f"specifications: {failure!r}."
        ) from failure


@cache
def _bars(symbol: str) -> tuple[Bar, ...]:
    """Every stored M1 bar for `symbol`, oldest first."""
    filename = f"{symbol}.csv"
    reader = csv.DictReader(_read_data(filename).splitlines())
    if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
        raise SourceUnavailable(
            f"Replay data file {filename!r} has columns {reader.fieldnames}, expected "
            f"{list(CSV_COLUMNS)}."
        )
    try:
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
    except (ValueError, TypeError) as failure:
        # Pydantic's ValidationError is a ValueError, so a row that parses but does not
        # validate, such as a naive timestamp, lands here too rather than escaping.
        raise SourceUnavailable(
            f"Replay data file {filename!r} has a row this source cannot read: {failure!r}."
        ) from failure


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
