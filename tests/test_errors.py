"""Tests for the two error channels.

MCP has two, with opposite semantics, and the whole point of the exception hierarchy is
choosing between them correctly.

An ordinary exception inside a tool body becomes a TOOL error: `is_error=True`, the message
reaches the model, and the model can correct itself. `MCPError` becomes a JSON-RPC protocol
error: no result at all, so the model sees nothing to act on. The test is whether a smarter
model could have avoided the failure.

The third case is the one that does not raise at all. A tool that RETURNS an error string
comes back with `is_error=False` and reads to the model, and to the user interface, as a
successful answer. `test_no_tool_reports_a_failure_as_a_success` calls every tool with
input it cannot serve and asserts the flag, which is the only way to catch that class of
mistake mechanically.
"""

from __future__ import annotations

from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any, NoReturn

import pytest
from mcp import Client, MCPError
from mcp.types import INTERNAL_ERROR

from quotez import replay
from quotez.config import ServerConfig
from quotez.errors import SourceUnavailable
from quotez.models import SourceName
from quotez.protocol import MarketDataSource
from quotez.replay import SPECS_FILE
from quotez.server import build_server
from tests.conftest import SYMBOL, TOOL_NAMES

WINDOW = {"start": "2026-06-01T08:00:00Z", "end": "2026-06-01T12:00:00Z"}

# One call per tool, each carrying input the server cannot serve. Tools that take no
# arguments cannot be given bad input, so they are exercised against a dead source below.
BAD_CALLS: dict[str, dict[str, Any]] = {
    "list_symbols": {"group": "*, !"},
    "get_quote": {"symbol": "NOT_A_SYMBOL"},
    "get_bars": {"symbol": "NOT_A_SYMBOL", "timeframe": "H1", "count": 5},
    "get_bars_range": {"symbol": "NOT_A_SYMBOL", "timeframe": "H1", **WINDOW},
    "symbol_info": {"symbol": "NOT_A_SYMBOL"},
}


def _terminal_is_gone() -> NoReturn:
    raise SourceUnavailable(
        "MetaTrader 5 initialize() failed with code -10005: IPC timeout.", code=-10005
    )


class DeadSource:
    """A source whose terminal stopped answering. Connecting works; every query fails.

    Written out method by method rather than through `__getattr__`, because a runtime
    protocol check uses `inspect.getattr_static` and would not see dynamic attributes. This
    class has to satisfy `MarketDataSource` for the tests below to mean anything.
    """

    @property
    def name(self) -> SourceName:
        return "mt5"

    @property
    def synthetic(self) -> bool:
        return False

    def connect(self) -> None:
        """The lifespan succeeds, so failures land on the tools rather than on startup."""

    def close(self) -> None:
        """Nothing to release."""

    def list_symbols(self, group: str | None = None) -> NoReturn:
        _terminal_is_gone()

    def get_quote(self, symbol: str) -> NoReturn:
        _terminal_is_gone()

    def get_bars(self, symbol: str, timeframe: str, count: int) -> NoReturn:
        _terminal_is_gone()

    def get_bars_range(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> NoReturn:
        _terminal_is_gone()

    def symbol_info(self, symbol: str) -> NoReturn:
        _terminal_is_gone()

    def get_account(self) -> NoReturn:
        _terminal_is_gone()

    def list_positions(self) -> NoReturn:
        _terminal_is_gone()

    def list_orders(self) -> NoReturn:
        _terminal_is_gone()


def dead_server(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        "quotez.server.make_source",
        lambda _config: DeadSource(),  # type: ignore[return-value]
    )
    return build_server(ServerConfig())


# --------------------------------------------------------------------------------------
# Tool errors: the model must be able to read and act on these


@pytest.mark.anyio
async def test_an_unknown_symbol_is_a_tool_error_not_a_protocol_error(client: Client) -> None:
    result = await client.call_tool("get_quote", {"symbol": "NOT_A_SYMBOL"})
    assert result.is_error is True
    assert result.structured_content is None
    text = result.content[0].text
    assert "NOT_A_SYMBOL" in text
    assert "not available on this server" in text


@pytest.mark.anyio
async def test_an_invalid_timeframe_is_rejected_before_the_handler_runs(
    client: Client,
) -> None:
    result = await client.call_tool("get_bars", {"symbol": SYMBOL, "timeframe": "W1", "count": 5})
    assert result.is_error is True
    text = result.content[0].text
    # A literal_error names the schema constraint, which only the SDK's pre-dispatch
    # validation produces. The handler's own message would have read differently.
    assert "literal_error" in text
    assert "'M1'" in text and "'D1'" in text


@pytest.mark.anyio
async def test_count_zero_is_rejected_by_the_schema(client: Client) -> None:
    result = await client.call_tool("get_bars", {"symbol": SYMBOL, "timeframe": "H1", "count": 0})
    assert result.is_error is True
    assert "greater than or equal to 1" in result.content[0].text


@pytest.mark.anyio
async def test_a_naive_datetime_is_rejected_with_an_example(client: Client) -> None:
    result = await client.call_tool(
        "get_bars_range",
        {
            "symbol": SYMBOL,
            "timeframe": "H1",
            "start": "2026-06-01T08:00:00",
            "end": "2026-06-01T12:00:00Z",
        },
    )
    assert result.is_error is True
    text = result.content[0].text
    assert "start has no timezone" in text
    assert "2026-06-01T08:00:00Z" in text


@pytest.mark.anyio
async def test_an_inverted_range_is_rejected(client: Client) -> None:
    result = await client.call_tool(
        "get_bars_range",
        {
            "symbol": SYMBOL,
            "timeframe": "H1",
            "start": WINDOW["end"],
            "end": WINDOW["start"],
        },
    )
    assert result.is_error is True
    assert "must be earlier than end" in result.content[0].text


@pytest.mark.anyio
@pytest.mark.parametrize("tool", sorted(BAD_CALLS))
async def test_no_tool_reports_a_failure_as_a_success(client: Client, tool: str) -> None:
    result = await client.call_tool(tool, BAD_CALLS[tool])
    # A returned error string would arrive with is_error=False and read as an answer.
    assert result.is_error is True, tool
    assert result.structured_content is None, tool
    assert result.content and result.content[0].text.startswith("Error executing tool"), tool


def test_every_tool_that_can_fail_is_covered() -> None:
    # get_account, list_positions and list_orders take no arguments, so bad input cannot
    # reach them; they are covered by the dead-source tests instead.
    assert set(BAD_CALLS) | {"get_account", "list_positions", "list_orders"} == set(TOOL_NAMES)


# --------------------------------------------------------------------------------------
# Protocol errors: no retry the model can make will fix these


@pytest.mark.anyio
@pytest.mark.parametrize("tool", TOOL_NAMES)
async def test_an_unavailable_source_raises_a_protocol_error(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    arguments: dict[str, Any] = {
        "list_symbols": {},
        "get_quote": {"symbol": SYMBOL},
        "get_bars": {"symbol": SYMBOL, "timeframe": "H1", "count": 5},
        "get_bars_range": {"symbol": SYMBOL, "timeframe": "H1", **WINDOW},
        "symbol_info": {"symbol": SYMBOL},
        "get_account": {},
        "list_positions": {},
        "list_orders": {},
    }[tool]
    async with Client(dead_server(monkeypatch), raise_exceptions=True) as client:
        with pytest.raises(MCPError) as caught:
            await client.call_tool(tool, arguments)
    code, message, _data = caught.value.args
    assert code == INTERNAL_ERROR == -32603
    assert "IPC timeout" in message


@pytest.mark.anyio
async def test_a_resource_read_against_an_unavailable_source_raises_a_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Resources have only the protocol path: there is no is_error flag on a resource read.
    async with Client(dead_server(monkeypatch), raise_exceptions=True) as client:
        with pytest.raises(MCPError) as caught:
            await client.read_resource("symbols://list")
    assert caught.value.args[0] == INTERNAL_ERROR


@pytest.mark.anyio
async def test_an_unreadable_data_file_is_a_protocol_error_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A corrupt bundled file takes the same channel a dead terminal does, through a client.

    The unit tests in test_replay.py assert the exception type. This asserts what a host
    actually receives, which is the part that matters: no argument the model sends will fix
    a file it cannot read, so the call must come back with no result rather than as a tool
    error it will try to work around.
    """
    for name in (SPECS_FILE, f"{SYMBOL}.csv"):
        (tmp_path / name).write_bytes(
            resources.files("quotez.data").joinpath(name).read_bytes()
            if name == SPECS_FILE
            else b"\xff\xfe corrupt"
        )
    monkeypatch.setattr(replay, "_data_dir", lambda: tmp_path)
    replay._bars.cache_clear()
    replay._specs.cache_clear()
    try:
        async with Client(build_server(ServerConfig()), raise_exceptions=True) as client:
            with pytest.raises(MCPError) as caught:
                await client.call_tool("get_quote", {"symbol": SYMBOL})
    finally:
        replay._bars.cache_clear()
        replay._specs.cache_clear()
    code, message, _data = caught.value.args
    assert code == INTERNAL_ERROR
    assert f"{SYMBOL}.csv" in message


@pytest.mark.anyio
async def test_an_unknown_resource_is_a_protocol_error(client: Client) -> None:
    with pytest.raises(MCPError) as caught:
        await client.read_resource("bars://SYNTH_FX_ALPHA/H1")
    code, message, data = caught.value.args
    assert code == -32602
    assert "bars://SYNTH_FX_ALPHA/H1" in message
    assert data == {"uri": "bars://SYNTH_FX_ALPHA/H1"}


def test_the_dead_source_still_satisfies_the_protocol() -> None:
    # Otherwise the tests above would be proving something about a shape the server never
    # actually accepts.
    assert isinstance(DeadSource(), MarketDataSource)
