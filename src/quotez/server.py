"""The MCP surface: eight read only tools and one resource, over stdio.

Nothing in this module knows what a MetaTrader terminal is. It is written against
`MarketDataSource`, and `build_server` picks the implementation, so the tools are exercised
identically whether the data comes from a terminal or from the bundled files.

Three decisions worth knowing before reading the code.

Tool order is the `tools/list` order and the SDK preserves registration order, so the eight
tools are registered in a fixed sequence rather than built from a dict or a set. Clients
cache the tool list and a stable order improves prompt cache hit rates.

Every handler is a plain `def`, never `async def`. mcp 2.x runs sync handlers on a worker
thread through `anyio.to_thread.run_sync`, which is exactly right for a blocking C
extension. A consequence: `asyncio.get_running_loop()` inside one of these now raises.

Errors take one of two channels and the choice is not cosmetic. An ordinary exception
becomes a tool error the model can read and correct from. `MCPError` becomes a JSON-RPC
protocol error with no result at all, so the model sees nothing. The test is whether a
smarter model could have avoided the failure: a misspelled symbol yes, a terminal that is
not running no. Nothing here ever RETURNS an error string, because a returned string
carries `is_error=False` and reads as a successful answer.

Stdout is the wire on stdio. There is no `print` in this package and nothing writes to
stdout at import time; logging goes to stderr, configured in `quotez.cli`.

Importing this module reads nothing and builds nothing. The `mcp` global that
`mcp run src/quotez/server.py` looks for is built on first attribute access, not at import,
because `quotez.cli` imports this module to reach `build_server` and an import that read
`QUOTEZ_*` would make a malformed variable fatal before argparse could apply the flag that
overrides it.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Annotated

from mcp import MCPError
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import INTERNAL_ERROR, ToolAnnotations
from pydantic import Field

from quotez import __version__
from quotez.aggregate import timeframe_seconds
from quotez.config import MAX_BARS_CEILING, ServerConfig
from quotez.errors import (
    InvalidRequest,
    QuotezError,
    SourceUnavailable,
    SymbolNotFound,
)
from quotez.models import (
    Account,
    BarSeries,
    OrderList,
    PositionList,
    Quote,
    SymbolList,
    SymbolSpec,
    Timeframe,
)
from quotez.mt5source import Mt5Source
from quotez.protocol import MarketDataSource
from quotez.replay import ReplaySource

__all__ = ["build_server", "make_source", "mcp"]

if TYPE_CHECKING:
    # `mcp` is produced at run time by the module `__getattr__` at the bottom of this file.
    # Declaring the type here is what lets a linter and a type checker see the name without
    # anything being built at import.
    mcp: MCPServer

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

GROUP_SYNTAX_HELP = (
    "MetaTrader group syntax: '*' wildcards at the start and end of a pattern, several "
    "comma separated conditions, and '!' to negate one. Inclusions must come before "
    'exclusions, so "*, !*USD*" is everything except the USD instruments while '
    '"!*USD*, *" matches everything.'
)


def make_source(config: ServerConfig) -> MarketDataSource:
    """The data source `config` asks for. Constructing it opens no connection.

    Importing `Mt5Source` is safe on any platform: the extension itself is resolved inside
    the source, on first use, not at module import.
    """
    return Mt5Source() if config.source == "mt5" else ReplaySource()


@contextmanager
def _protocol_errors() -> Iterator[None]:
    """Sort this server's failures into the two kinds the protocol distinguishes.

    A DEAD SOURCE IS A PROTOCOL ERROR. A missing terminal is not something the model can retry
    its way out of, so it must not come back as a readable tool result that invites another
    attempt.

    EVERYTHING ELSE IS AN ANTICIPATED TOOL ERROR AND IS RAISED AS ONE, which is a correction
    with a measured cause. These used to escape as ordinary exceptions and the SDK rendered
    their messages to the caller, so a refused symbol was named and this file's docstrings said
    so. That is no longer true above mcp 2.0: from 2.1 the server masks any exception it did not
    expect, and every refusal here came back as the bare string "Error executing tool get_quote"
    with the symbol gone. Measured on the Dependabot pull request that raised the pin, where six
    of seven jobs failed on exactly that.

    The SDK is right and the modelling here was wrong. `ToolError` is its channel for a failure
    the author anticipated and intends the caller to read, and a refusal is the most anticipated
    thing this server does. Raising it explicitly says so, keeps the wording under this
    repository's control rather than the SDK's, and works on both 2.0 and 2.1.

    THE WORDING IS STILL IDENTICAL for a symbol the operator hid and one that never existed,
    which is the property that stops the error being used to enumerate what is hidden. That
    property lives in `errors.py`, not here, and a test asserts it.
    """
    try:
        yield
    except SourceUnavailable as failure:
        raise MCPError(code=INTERNAL_ERROR, message=str(failure)) from failure
    except QuotezError as failure:
        raise ToolError(str(failure)) from failure


def build_server(config: ServerConfig) -> MCPServer:
    """Assemble the server described by `config`. No I/O happens until the lifespan runs."""
    source = make_source(config)

    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[MarketDataSource]:
        # The terminal connection is opened exactly once, here, and never inside a tool.
        # `mt5.initialize()` starts the terminal if it is not running, bounded by a timeout
        # that defaults to 60000 milliseconds, so doing it per call would risk making the
        # first tool call look hung.
        source.connect()
        try:
            yield source
        finally:
            source.close()

    mcp = MCPServer(
        "QUOTEZ",
        title="QUOTEZ market data",
        version=__version__,
        instructions=(
            "Read-only MetaTrader 5 market data. No order placement, modification or "
            "cancellation exists in this server. Call list_symbols first to learn the "
            "instrument universe. All times are UTC and every bar is labelled by its open "
            f"(left edge). get_bars is capped at {config.max_bars} bars per call; use a "
            "coarser timeframe rather than more bars. On the replay source every price is "
            "generated, not recorded, and every payload carries synthetic=true."
        ),
        lifespan=lifespan,
    )

    def permitted(symbol: str) -> str:
        """Reject a symbol the operator did not expose, in the same words as a typo.

        A distinct "not permitted" message would turn the whitelist into a discovery oracle
        for instruments the operator deliberately hid.
        """
        if not config.allows(symbol):
            raise SymbolNotFound(symbol)
        return symbol

    def symbol_universe(group: str | None = None) -> SymbolList:
        with _protocol_errors():
            listed = source.list_symbols(group)
        # The whitelist is applied above the source, so no source can widen the exposed
        # universe, and it is applied to listing as well as to the getters. Filtering only
        # the getters would let the model discover instruments it is then refused.
        symbols = [entry for entry in listed.symbols if config.allows(entry.name)]
        return listed.model_copy(update={"symbols": symbols, "count": len(symbols)})

    # ----------------------------------------------------------------------------------
    # Tools. Registration order is the tools/list order; do not reorder casually.

    @mcp.tool(title="List instruments", annotations=READ_ONLY)
    def list_symbols(
        group: Annotated[
            str | None,
            Field(description=f"Optional filter. {GROUP_SYNTAX_HELP}"),
        ] = None,
    ) -> SymbolList:
        """Return every instrument this server exposes, with its digits and point size.

        Call this before anything else: it is the only authoritative list of symbol names,
        and a name that is not in it produces a tool error everywhere else. The optional
        group filter uses MetaTrader's own syntax, described in the argument. On the replay
        source the instruments are generated and the payload carries synthetic=true.
        """
        with _protocol_errors():
            return symbol_universe(group)

    @mcp.tool(title="Get a quote", annotations=READ_ONLY)
    def get_quote(
        symbol: Annotated[
            str, Field(description="Instrument name exactly as list_symbols spells it.")
        ],
    ) -> Quote:
        """Return the latest bid, ask and spread in points for one instrument.

        The time is UTC. On the replay source it is the last stored bar's open time rather
        than the current clock, so the answer is reproducible and is NOT a live market
        price. An unknown or unavailable symbol returns a tool error naming the symbol;
        call list_symbols if unsure.
        """
        with _protocol_errors():
            return source.get_quote(permitted(symbol))

    @mcp.tool(title="Get recent bars", annotations=READ_ONLY)
    def get_bars(
        symbol: Annotated[
            str, Field(description="Instrument name exactly as list_symbols spells it.")
        ],
        timeframe: Timeframe,
        count: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_BARS_CEILING,
                description="How many of the most recent bars to return, newest last.",
            ),
        ] = 100,
    ) -> BarSeries:
        """Return the most recent OHLCV bars for a symbol, oldest first.

        Times are UTC and label each bar's OPEN, the left edge of the interval it covers.
        `count` is capped by the server (see the server instructions for the current
        limit); ask for a coarser timeframe rather than more bars. The bar that is still
        forming is never returned, so the newest bar is always a closed one; call get_quote
        for the current price. An unknown or unavailable symbol returns a tool error naming
        the symbol; call list_symbols first if unsure. On the replay source the prices are
        generated, not recorded from any market.
        """
        with _protocol_errors():
            if count > config.max_bars:
                raise InvalidRequest(
                    f"count {count} exceeds this server's limit of {config.max_bars} bars per "
                    "call. Request fewer bars or a coarser timeframe."
                )
            with _protocol_errors():
                return source.get_bars(permitted(symbol), timeframe, count)

    @mcp.tool(title="Get bars in a date range", annotations=READ_ONLY)
    def get_bars_range(
        symbol: Annotated[
            str, Field(description="Instrument name exactly as list_symbols spells it.")
        ],
        timeframe: Timeframe,
        start: Annotated[datetime, Field(description="Inclusive, ISO 8601, UTC.")],
        end: Annotated[datetime, Field(description="Exclusive, ISO 8601, UTC.")],
    ) -> BarSeries:
        """Return the OHLCV bars whose open time falls in [start, end), oldest first.

        Both bounds must carry a UTC offset, for example 2026-06-01T08:00:00Z. `start` is
        inclusive and `end` is exclusive, so consecutive ranges tile without repeating a
        bar. The number of bars the range spans is capped by the same limit that applies to
        get_bars, so a wide window at a fine timeframe returns a tool error asking for a
        coarser one rather than a truncated answer. Unlike get_bars, an `end` that reaches
        into the interval currently forming can return that bar, because the bounds are
        yours; stop `end` at a closed interval if that matters. On the replay source the
        prices are generated, not recorded from any market.
        """
        with _protocol_errors():
            for name, moment in (("start", start), ("end", end)):
                if moment.tzinfo is None:
                    raise InvalidRequest(
                        f"{name} has no timezone. Pass a UTC time, for example "
                        f"2026-06-01T08:00:00Z."
                    )
            if start >= end:
                raise InvalidRequest(
                    f"start {start.isoformat()} must be earlier than end {end.isoformat()}."
                )
            # Counted before fetching. Without this a wide date range walks straight past the
            # cap that get_bars enforces on `count`.
            span = math.ceil((end - start).total_seconds() / timeframe_seconds(timeframe))
            if span > config.max_bars:
                raise InvalidRequest(
                    f"That range spans up to {span} {timeframe} bars, above this server's limit "
                    f"of {config.max_bars} per call. Narrow the range or use a coarser timeframe."
                )
            with _protocol_errors():
                return source.get_bars_range(permitted(symbol), timeframe, start, end)

    @mcp.tool(title="Get contract specification", annotations=READ_ONLY)
    def symbol_info(
        symbol: Annotated[
            str, Field(description="Instrument name exactly as list_symbols spells it.")
        ],
    ) -> SymbolSpec:
        """Return the contract specification for one instrument.

        Digits and point size for rounding prices, current spread, minimum stop distance,
        tick value and size, contract size, the tradable volume range, and the base, profit
        and margin currencies. Field names are MetaTrader's own. An unknown or unavailable
        symbol returns a tool error naming the symbol. On the replay source the instrument
        is generated and the payload carries synthetic=true.
        """
        with _protocol_errors():
            return source.symbol_info(permitted(symbol))

    @mcp.tool(title="Get account state", annotations=READ_ONLY)
    def get_account() -> Account:
        """Return the connected account's balance, equity, margin and leverage.

        The login is masked to its last four digits and the broker, server and account
        holder names are never returned. On the replay source these figures are invented
        placeholders describing no real account: the payload carries synthetic=true, the
        currency is SYN and the login is ****0000. Do not restate them as a real balance.
        """
        with _protocol_errors():
            return source.get_account()

    @mcp.tool(title="List open positions", annotations=READ_ONLY)
    def list_positions() -> PositionList:
        """Return every open position, with entry price, current price and floating profit.

        Read only: this server can open, modify and close nothing. On the replay source the
        list is always empty and the payload carries synthetic=true.
        """
        with _protocol_errors():
            return source.list_positions()

    @mcp.tool(title="List pending orders", annotations=READ_ONLY)
    def list_orders() -> OrderList:
        """Return every pending order, with its type, volumes and trigger price.

        Read only: this server can place, modify and cancel nothing. On the replay source
        the list is always empty and the payload carries synthetic=true.
        """
        with _protocol_errors():
            return source.list_orders()

    # ----------------------------------------------------------------------------------
    # Resource. A tool is what the MODEL decides to call; a resource is what the
    # APPLICATION decides to load. The instrument universe is the second kind, so it is
    # offered as a concrete URI a host can pin into context. It is a plain string with an
    # explicit mime type rather than a model, because the resource read path has no
    # structured output channel.

    @mcp.resource(
        "symbols://list",
        name="Instrument universe",
        title="Available instruments",
        description="The full list of instruments this server exposes, as JSON.",
        mime_type="application/json",
    )
    def symbols_resource() -> str:
        """Return the instrument universe as JSON for context loading."""
        with _protocol_errors():
            return symbol_universe().model_dump_json(indent=2)

    return mcp


# Holds the global below once something has asked for it. Nothing builds it eagerly.
_default_server: MCPServer | None = None


def __getattr__(name: str) -> MCPServer:
    """Build the module level `mcp` global on first access rather than at import.

    `mcp run src/quotez/server.py` resolves the server by name, and both the `hasattr` probe
    and the `getattr` that follows it fall through to here, so that entry point is unchanged
    and still configured entirely by `QUOTEZ_*`.

    What changes is the console script. `quotez.cli` imports this module for `build_server`,
    so a global built at import made every `QUOTEZ_*` variable fatal before `main` ran:
    `QUOTEZ_MAX_BARS=abc quotez --max-bars 50` died with a traceback and exit 1 despite the
    flag that replaced the offending value, and so did `--help` and `--version`. Argument
    errors are argparse's to report, and argparse exits 2.
    """
    if name != "mcp":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global _default_server
    if _default_server is None:
        _default_server = build_server(ServerConfig.from_env())
    return _default_server
