"""Tests for the MCP surface itself.

These go through the SDK's in-process client, so they assert what a host would actually
receive: the tool list and its order, the annotations, the generated schemas, the
structured content, and the resource. Calling the handlers directly would skip all of it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.types import Tool

from quotez import __version__
from quotez.config import ServerConfig
from quotez.models import Quote, SymbolSpec
from quotez.server import build_server
from tests.conftest import SYMBOL, TOOL_NAMES, connected

SOURCE_DIR = Path(__file__).resolve().parent.parent / "src" / "quotez"

# The three MetaTrader calls that would make the read-only claim a matter of discipline
# instead of a property of the code.
WRITE_CALLS = ("order_send", "order_check", "symbol_select")


async def tool_map(client: Client) -> dict[str, Tool]:
    return {tool.name: tool for tool in (await client.list_tools()).tools}


# --------------------------------------------------------------------------------------
# The tool list


@pytest.mark.anyio
async def test_tools_list_contains_exactly_the_eight_expected_names(client: Client) -> None:
    listed = [tool.name for tool in (await client.list_tools()).tools]
    assert listed == list(TOOL_NAMES)


@pytest.mark.anyio
async def test_tool_order_is_the_registration_order_across_builds() -> None:
    # tools/list order is what clients cache, and a stable order improves prompt cache hit
    # rates. Building the list from a set or a dict comprehension would pass once and drift.
    async with connected() as first, connected(ServerConfig(max_bars=42)) as second:
        assert [tool.name for tool in (await first.list_tools()).tools] == list(TOOL_NAMES)
        assert [tool.name for tool in (await second.list_tools()).tools] == list(TOOL_NAMES)


@pytest.mark.anyio
async def test_every_tool_is_declared_read_only_and_closed_world(client: Client) -> None:
    for tool in (await client.list_tools()).tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.open_world_hint is False, tool.name
        # destructive_hint and idempotent_hint are defined by the spec only for tools that
        # are not read only, so leaving them unset is correct rather than an omission.
        assert tool.annotations.destructive_hint is None, tool.name
        assert tool.annotations.idempotent_hint is None, tool.name


@pytest.mark.anyio
async def test_every_tool_has_a_title_a_description_and_an_output_schema(
    client: Client,
) -> None:
    for tool in (await client.list_tools()).tools:
        assert tool.title, tool.name
        # The docstring IS the tool description the model reads.
        assert tool.description and len(tool.description) > 60, tool.name
        assert tool.output_schema, tool.name
        assert tool.output_schema.get("type") == "object", tool.name


@pytest.mark.anyio
async def test_every_tool_reads_and_none_offers_to_write(client: Client) -> None:
    for tool in (await client.list_tools()).tools:
        # The whole surface is getters and listers. A mutating tool could not be named
        # inside this scheme without the name itself giving it away.
        assert tool.name.startswith(("get_", "list_")) or tool.name == "symbol_info"
        assert tool.title is not None and tool.title.split()[0] in {"Get", "List"}
        # The write calls are not offered, not described and not hinted at.
        for call in WRITE_CALLS:
            assert call not in f"{tool.name} {tool.title} {tool.description}"


def test_the_source_tree_contains_no_write_path() -> None:
    # The read-only claim is structural, not a setting: these calls are absent from the
    # package, so no configuration can turn them on.
    sources = sorted(SOURCE_DIR.rglob("*.py"))
    assert sources
    for call in WRITE_CALLS:
        offenders = [
            path.name
            for path in sources
            if f"{call}(" in path.read_text(encoding="utf-8")
            or f".{call}" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], (call, offenders)


# --------------------------------------------------------------------------------------
# Server metadata


@pytest.mark.anyio
async def test_instructions_name_the_bar_cap_and_the_read_only_scope() -> None:
    async with connected(ServerConfig(max_bars=321)) as client:
        instructions = client.instructions
    assert instructions
    assert "321" in instructions
    assert "No order placement" in instructions
    assert "list_symbols first" in instructions
    assert "UTC" in instructions


@pytest.mark.anyio
async def test_the_server_identifies_itself_with_the_package_version(client: Client) -> None:
    info = client.server_info
    assert info is not None
    assert info.name == "QUOTEZ"
    assert info.title == "QUOTEZ market data"
    assert info.version == __version__


def test_building_an_mt5_server_touches_no_terminal() -> None:
    # Construction is pure: the connection belongs to the lifespan, because
    # mt5.initialize() launches the terminal and waits up to 60 seconds. Building the mt5
    # server on a machine with no wheel available has to work, or `quotez --source mt5
    # --help` would fail on macOS.
    build_server(ServerConfig(source="mt5"))
    assert "MetaTrader5" not in sys.modules


# --------------------------------------------------------------------------------------
# Payload shapes


@pytest.mark.anyio
async def test_get_quote_structured_content_validates_against_the_quote_model(
    client: Client,
) -> None:
    result = await client.call_tool("get_quote", {"symbol": SYMBOL})
    assert result.is_error is False
    assert result.structured_content is not None
    quote = Quote.model_validate(result.structured_content)
    assert quote.symbol == SYMBOL
    assert quote.source == "replay"
    assert quote.synthetic is True
    assert quote.time.tzinfo is not None


@pytest.mark.anyio
async def test_get_bars_returns_an_unwrapped_model_not_a_result_envelope(
    client: Client,
) -> None:
    result = await client.call_tool("get_bars", {"symbol": SYMBOL, "timeframe": "H1", "count": 3})
    payload = result.structured_content
    assert payload is not None
    # A BaseModel return is used unwrapped; a scalar or a list would have arrived as
    # {"result": ...} instead.
    assert "result" not in payload
    assert set(payload) == {"symbol", "timeframe", "source", "synthetic", "count", "bars"}
    assert payload["count"] == 3
    assert len(payload["bars"]) == 3
    assert all(bar["spread"] is None for bar in payload["bars"])


@pytest.mark.anyio
async def test_symbol_info_returns_every_declared_field(client: Client) -> None:
    result = await client.call_tool("symbol_info", {"symbol": SYMBOL})
    payload = result.structured_content
    assert payload is not None
    assert set(payload) == set(SymbolSpec.model_fields)
    SymbolSpec.model_validate(payload)


@pytest.mark.anyio
async def test_the_bar_cap_appears_in_the_get_bars_input_schema(client: Client) -> None:
    schema = (await tool_map(client))["get_bars"].input_schema
    count = schema["properties"]["count"]
    # The bound is in the schema, not only in the handler, so the model can read it before
    # calling and the SDK enforces it before the handler runs.
    assert count["minimum"] == 1
    assert count["maximum"] == 5000
    assert count["default"] == 100
    assert schema["properties"]["timeframe"]["enum"] == [
        "M1",
        "M5",
        "M15",
        "M30",
        "H1",
        "H4",
        "D1",
    ]


@pytest.mark.anyio
async def test_the_group_filter_syntax_is_documented_in_the_input_schema(
    client: Client,
) -> None:
    schema = (await tool_map(client))["list_symbols"].input_schema
    description = schema["properties"]["group"]["description"]
    assert "!" in description
    assert "Inclusions must come before" in description


# --------------------------------------------------------------------------------------
# The resource


@pytest.mark.anyio
async def test_resources_list_contains_the_concrete_symbols_uri(client: Client) -> None:
    listed = (await client.list_resources()).resources
    by_uri = {str(resource.uri): resource for resource in listed}
    assert set(by_uri) == {"symbols://list"}
    universe = by_uri["symbols://list"]
    assert universe.name == "Instrument universe"
    assert universe.title == "Available instruments"
    assert universe.mime_type == "application/json"


@pytest.mark.anyio
async def test_no_resource_templates_are_registered(client: Client) -> None:
    # bars://{symbol}/{timeframe} was deliberately not built: it duplicates get_bars, and a
    # URI with placeholders leaves resources/list for resources/templates/list, which many
    # hosts surface poorly or not at all.
    assert (await client.list_resource_templates()).resource_templates == []


@pytest.mark.anyio
async def test_the_resource_payload_is_json_and_matches_list_symbols(client: Client) -> None:
    contents = (await client.read_resource("symbols://list")).contents
    assert len(contents) == 1
    assert contents[0].mime_type == "application/json"
    payload = json.loads(contents[0].text)
    listed = (await client.call_tool("list_symbols", {})).structured_content
    assert payload == listed
