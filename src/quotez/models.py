"""Output models, which are also the tools' output schemas.

In mcp 2.x the return annotation IS the contract. The SDK derives `outputSchema` from it,
validates the returned object against that schema before it leaves the server, and puts
the result in `structuredContent`. A `BaseModel` is used unwrapped, so `get_bars` returns
an object with a `bars` key rather than `{"result": ...}`. Returning text blobs instead
throws all of that away and leaves the model parsing prose.

Every field carries a description because the descriptions travel in the schema the model
reads. Units and conventions belong there, not in a comment.

Two fields appear on every payload: `source` names the implementation that produced the
numbers and `synthetic` says whether they were generated rather than observed. Both are
required. A model that reads an equity figure will restate it as fact, and a payload with
no marker gives it nothing to qualify the statement with.

Timestamps are UTC aware everywhere and every bar is labelled by its OPEN, the left edge
of the interval. Naive datetimes are rejected at construction rather than assumed to be
UTC: `datetime.timestamp()` reads a naive value as LOCAL time, which is the standard way
MetaTrader data ends up silently shifted by an hour.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

__all__ = [
    "Account",
    "Bar",
    "BarSeries",
    "Order",
    "OrderList",
    "Position",
    "PositionList",
    "Quote",
    "SourceName",
    "SymbolList",
    "SymbolSpec",
    "SymbolSummary",
    "Timeframe",
    "UtcDatetime",
]

SourceName = Literal["replay", "mt5"]
Timeframe = Literal["M1", "M5", "M15", "M30", "H1", "H4", "D1"]


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError(
            "timestamp must carry a UTC offset, for example 2026-06-01T08:00:00Z; "
            "a naive value would be read as local time"
        )
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]


class SymbolSummary(BaseModel):
    """One instrument as it appears in a listing."""

    name: str = Field(description="Symbol name exactly as the source spells it.")
    description: str = Field(description="Human readable instrument name.")
    digits: int = Field(description="Decimal places in a quoted price.")
    point: float = Field(description="Value of one point, the smallest price step.")


class SymbolList(BaseModel):
    """The instrument universe this server exposes, after any filtering."""

    source: SourceName = Field(description="Data source that produced this list.")
    synthetic: bool = Field(description="True when the instruments are generated, not real.")
    count: int = Field(description="Number of instruments returned.")
    symbols: list[SymbolSummary] = Field(description="The instruments, in source order.")


class Quote(BaseModel):
    """The latest bid and ask for one instrument."""

    symbol: str = Field(description="Symbol this quote belongs to.")
    time: UtcDatetime = Field(
        description="Quote time in UTC. On the replay source this is the last stored bar's "
        "open time, never the wall clock."
    )
    bid: float = Field(description="Best bid price.")
    ask: float = Field(description="Best ask price.")
    spread_points: int = Field(description="Ask minus bid, expressed in points.")
    source: SourceName = Field(description="Data source that produced this quote.")
    synthetic: bool = Field(description="True when the price is generated, not observed.")


class Bar(BaseModel):
    """One OHLCV bar, labelled by its opening time."""

    time: UtcDatetime = Field(
        description="Bar OPEN time in UTC, the left edge of the interval it covers."
    )
    open: float = Field(description="First traded price in the interval.")
    high: float = Field(description="Highest price in the interval.")
    low: float = Field(description="Lowest price in the interval.")
    close: float = Field(description="Last traded price in the interval.")
    tick_volume: int = Field(description="Number of price changes in the interval.")
    spread: int | None = Field(
        description="Spread in points, present on M1 bars only. Null on any aggregated "
        "timeframe, because a spread is a point in time quote property and summing it "
        "across an interval would be meaningless."
    )


class BarSeries(BaseModel):
    """A run of bars for one instrument at one timeframe, oldest first."""

    symbol: str = Field(description="Symbol these bars belong to.")
    timeframe: Timeframe = Field(description="Timeframe of each bar.")
    source: SourceName = Field(description="Data source that produced these bars.")
    synthetic: bool = Field(description="True when the prices are generated, not observed.")
    count: int = Field(description="Number of bars returned.")
    bars: list[Bar] = Field(description="The bars, oldest first.")


class SymbolSpec(BaseModel):
    """Contract specification for one instrument.

    Field names are copied verbatim from `MetaTrader5.symbol_info()` so the mapping from
    terminal to payload can be audited by eye.
    """

    name: str = Field(description="Symbol name.")
    description: str = Field(description="Human readable instrument name.")
    digits: int = Field(description="Decimal places in a quoted price.")
    point: float = Field(description="Value of one point, the smallest price step.")
    spread: int = Field(description="Current spread in points.")
    spread_float: bool = Field(description="True when the broker quotes a floating spread.")
    trade_stops_level: int = Field(
        description="Minimum distance in points between price and a stop or limit order."
    )
    trade_freeze_level: int = Field(
        description="Distance in points within which orders are frozen and cannot be changed."
    )
    trade_tick_value: float = Field(
        description="Profit in the account currency from a one tick move on one lot."
    )
    trade_tick_size: float = Field(description="Smallest price change, in price units.")
    trade_contract_size: float = Field(description="Units of the base asset in one lot.")
    volume_min: float = Field(description="Smallest tradable volume, in lots.")
    volume_max: float = Field(description="Largest tradable volume, in lots.")
    volume_step: float = Field(description="Volume increment, in lots.")
    currency_base: str = Field(description="Base currency of the instrument.")
    currency_profit: str = Field(description="Currency the profit is denominated in.")
    currency_margin: str = Field(description="Currency the margin is charged in.")
    source: SourceName = Field(description="Data source that produced this specification.")
    synthetic: bool = Field(description="True when the instrument is generated, not real.")


class Account(BaseModel):
    """Account state, with everything identifying removed.

    The broker name, the server name and the account holder's name are never returned at
    all. They identify the person and the broker and no market data question needs them.
    """

    login_masked: str = Field(
        description="Account login masked to its last four digits. The full login is never "
        "returned."
    )
    currency: str = Field(description="Account deposit currency.")
    balance: float = Field(description="Balance, excluding floating profit and loss.")
    equity: float = Field(description="Balance plus floating profit and loss.")
    margin: float = Field(description="Margin currently in use.")
    margin_free: float = Field(description="Margin available for new positions.")
    margin_level: float = Field(description="Equity divided by margin, as a percentage.")
    leverage: int = Field(description="Account leverage, for example 100 for 1:100.")
    source: SourceName = Field(description="Data source that produced these figures.")
    synthetic: bool = Field(
        description="True when the figures are generated. The replay source always sets this, "
        "and its balance and equity are invented placeholders that describe no real account."
    )


class Position(BaseModel):
    """One open position."""

    ticket: int = Field(description="Position ticket, unique on the trade server.")
    symbol: str = Field(description="Symbol the position is held in.")
    type: Literal["buy", "sell"] = Field(description="Direction of the position.")
    volume: float = Field(description="Position size in lots.")
    price_open: float = Field(description="Price the position was opened at.")
    price_current: float = Field(description="Current market price used for valuation.")
    sl: float = Field(description="Stop loss price, 0 when none is set.")
    tp: float = Field(description="Take profit price, 0 when none is set.")
    profit: float = Field(description="Floating profit or loss in the account currency.")
    time: UtcDatetime = Field(description="Time the position was opened, UTC.")


class PositionList(BaseModel):
    """Every open position, read only."""

    source: SourceName = Field(description="Data source that produced this list.")
    synthetic: bool = Field(description="True when the positions are generated, not real.")
    count: int = Field(description="Number of open positions.")
    positions: list[Position] = Field(description="The open positions.")


class Order(BaseModel):
    """One pending order."""

    ticket: int = Field(description="Order ticket, unique on the trade server.")
    symbol: str = Field(description="Symbol the order is placed on.")
    type: str = Field(description="MetaTrader order type name, for example buy_limit.")
    volume_initial: float = Field(description="Volume the order was placed with, in lots.")
    volume_current: float = Field(description="Volume still pending, in lots.")
    price_open: float = Field(description="Price the order will trigger at.")
    sl: float = Field(description="Stop loss price, 0 when none is set.")
    tp: float = Field(description="Take profit price, 0 when none is set.")
    time_setup: UtcDatetime = Field(description="Time the order was placed, UTC.")


class OrderList(BaseModel):
    """Every pending order, read only."""

    source: SourceName = Field(description="Data source that produced this list.")
    synthetic: bool = Field(description="True when the orders are generated, not real.")
    count: int = Field(description="Number of pending orders.")
    orders: list[Order] = Field(description="The pending orders.")
