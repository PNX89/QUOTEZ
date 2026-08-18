"""QUOTEZ: MetaTrader 5 market data as typed, read only MCP tools.

Read `quotez.protocol` first. `MarketDataSource` is the seam the entire server is written
against, and it is why the bundled replay source is a real implementation rather than a
mock: nothing above it imports MetaTrader5, so the suite runs anywhere.

This package contains no write path. There is no `order_send`, no `order_check`, no
`symbol_select`, no MarketWatch mutation and no file write, which is a property of the
code rather than of a setting.

Importing `quotez` never imports MetaTrader5. `Mt5Source` resolves the extension lazily,
so `import quotez` works on macOS and Linux where no wheel exists.

The types and errors are re-exported here. The algorithms stay in their own modules
(`quotez.aggregate`, `quotez.groups`) because that is where their invariants are
documented and where the tests point.
"""

from __future__ import annotations

from quotez import aggregate, errors, groups, models, protocol
from quotez.aggregate import TIMEFRAME_SECONDS
from quotez.errors import InvalidRequest, QuotezError, SourceUnavailable, SymbolNotFound
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
from quotez.protocol import MarketDataSource

__version__ = "0.1.0"

__all__ = [
    "TIMEFRAME_SECONDS",
    "Account",
    "Bar",
    "BarSeries",
    "InvalidRequest",
    "MarketDataSource",
    "Order",
    "OrderList",
    "Position",
    "PositionList",
    "Quote",
    "QuotezError",
    "SourceName",
    "SourceUnavailable",
    "SymbolList",
    "SymbolNotFound",
    "SymbolSpec",
    "SymbolSummary",
    "Timeframe",
    "__version__",
    "aggregate",
    "errors",
    "groups",
    "models",
    "protocol",
]
