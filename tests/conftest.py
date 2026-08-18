"""Shared fixtures for the tests that go through a real MCP client.

The client is the SDK's in-process one, so a test exercises the whole path a host takes:
schema validation, the sync handler dispatched to a worker thread, the return value
validated against the generated output schema, and the two error channels. Assertions on a
handler called directly would skip every one of those.

`anyio_backend` is the anyio pytest plugin's hook, which ships with anyio and is therefore
already present through `mcp`. That is why there is no pytest-asyncio here. Async tests
carry `@pytest.mark.anyio`.

`raise_exceptions=True` only unsanitises failures raised OUTSIDE a tool body. An exception
inside a tool still comes back as `is_error=True` either way, which is exactly what the
error tests assert.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from mcp import Client

from quotez.config import ServerConfig
from quotez.server import build_server

REPLAY_SYMBOLS = (
    "SYNTH_FX_ALPHA",
    "SYNTH_FX_BETA",
    "SYNTH_IDX_GAMMA",
    "SYNTH_MTL_DELTA",
)
SYMBOL = REPLAY_SYMBOLS[0]

TOOL_NAMES = (
    "list_symbols",
    "get_quote",
    "get_bars",
    "get_bars_range",
    "symbol_info",
    "get_account",
    "list_positions",
    "list_orders",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def connected(config: ServerConfig | None = None) -> AsyncIterator[Client]:
    """A client talking to a freshly built server. Runs the lifespan on entry and exit."""
    async with Client(build_server(config or ServerConfig()), raise_exceptions=True) as client:
        yield client


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    """A client against the default replay server: every symbol, 1000 bar cap."""
    async with connected() as ready:
        yield ready
