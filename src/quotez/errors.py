"""The exception hierarchy, and which MCP error channel each exception belongs to.

MCP has two error channels with opposite semantics, and picking the wrong one is not a
cosmetic mistake.

An ordinary exception raised inside a tool body becomes a TOOL error: the call returns
with `is_error=True` and the message reaches the model, which can then correct itself and
try again. `MCPError` becomes a JSON-RPC protocol error instead: there is no result at
all, and the model sees nothing it can act on.

The test for which to use is whether a smarter model could have avoided the failure. A
misspelled symbol or an illegal date range, yes, so those raise ordinary exceptions.
A MetaTrader terminal that is not running, no, so that raises `MCPError` at the call
site. The mapping lives in `quotez.server`; this module only defines the types.

Nothing here is ever RETURNED from a tool. A returned error string carries
`is_error=False` and reads to both the model and the user interface as a successful
answer, which is how a tool ends up reporting a failure as fact.
"""

from __future__ import annotations

__all__ = [
    "InvalidRequest",
    "QuotezError",
    "SourceUnavailable",
    "SymbolNotFound",
]


class QuotezError(Exception):
    """Base class for every error QUOTEZ raises on purpose."""


class SymbolNotFound(QuotezError):
    """Raised when a symbol is unknown to the source or excluded by the whitelist.

    The message is identical in both cases deliberately. A distinct "not permitted"
    message would let a caller enumerate the instruments an operator chose not to expose,
    turning the whitelist into a discovery oracle.
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        super().__init__(f"Symbol {symbol!r} is not available on this server.")


class InvalidRequest(QuotezError):
    """Raised when arguments are well typed but out of contract.

    Unknown timeframe, inverted or oversized date range, a bar count above the configured
    cap. Every message names the bound that was crossed, because the model retries with
    whatever the message tells it.
    """


class SourceUnavailable(QuotezError):
    """Raised when the data source itself cannot serve any request.

    MetaTrader5 not importable, terminal not running, `initialize()` returning False.
    `code` carries the terminal's own `last_error()` code when there is one.
    """

    def __init__(self, message: str, *, code: int | None = None) -> None:
        self.code = code
        super().__init__(message)
