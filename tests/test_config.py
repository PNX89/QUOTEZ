"""Tests for quotez.config and the guardrails it feeds.

Two properties matter more than the parsing details. An illegal value must fail at startup
rather than at the first tool call, and it must be rejected rather than clamped: quietly
serving 5000 bars to a caller who asked for 50000 is how a bound stops meaning anything.

The whitelist tests reach through a real client, because applying the whitelist only to the
getters is the interesting bug. A model that can list an instrument and is then refused it
has been told the instrument exists.
"""

from __future__ import annotations

import json

import pytest
from mcp import Client

from quotez.config import (
    MAX_BARS_CEILING,
    MAX_BARS_DEFAULT,
    ServerConfig,
    parse_symbols,
)
from quotez.errors import InvalidRequest
from tests.conftest import REPLAY_SYMBOLS, SYMBOL, connected

BLOCKED = "SYNTH_IDX_GAMMA"


# --------------------------------------------------------------------------------------
# Values


def test_defaults() -> None:
    config = ServerConfig()
    assert config.source == "replay"
    assert config.max_bars == MAX_BARS_DEFAULT == 1000
    assert config.symbols == ()
    assert config.log_level == "INFO"


def test_a_value_above_the_ceiling_is_rejected_not_clamped() -> None:
    with pytest.raises(InvalidRequest) as caught:
        ServerConfig(max_bars=MAX_BARS_CEILING + 1)
    message = str(caught.value)
    assert str(MAX_BARS_CEILING) in message
    assert "coarser timeframe" in message
    assert ServerConfig(max_bars=MAX_BARS_CEILING).max_bars == MAX_BARS_CEILING


def test_a_non_positive_bar_cap_is_rejected() -> None:
    with pytest.raises(InvalidRequest, match="between 1"):
        ServerConfig(max_bars=0)


def test_an_unknown_source_is_rejected() -> None:
    with pytest.raises(InvalidRequest, match="Unknown source"):
        ServerConfig(source="binance")  # type: ignore[arg-type]


def test_an_unknown_log_level_is_rejected() -> None:
    with pytest.raises(InvalidRequest, match="Unknown log level"):
        ServerConfig(log_level="CHATTY")


def test_symbols_are_parsed_case_insensitively() -> None:
    assert parse_symbols(None) == ()
    assert parse_symbols("") == ()
    assert parse_symbols(" eurusd , gbpusd ,, ") == ("EURUSD", "GBPUSD")
    config = ServerConfig(symbols=("synth_fx_alpha",))
    assert config.symbols == ("SYNTH_FX_ALPHA",)
    assert config.allows("SYNTH_FX_ALPHA")
    assert config.allows("synth_fx_alpha")
    assert not config.allows("SYNTH_FX_BETA")


def test_an_empty_whitelist_allows_everything() -> None:
    config = ServerConfig()
    assert all(config.allows(name) for name in (*REPLAY_SYMBOLS, "ANYTHING_AT_ALL"))


# --------------------------------------------------------------------------------------
# Environment and flags


def test_the_environment_configures_the_module_global_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `mcp run` forwards no flags, so QUOTEZ_* is the only way to configure that entry point.
    monkeypatch.setenv("QUOTEZ_MAX_BARS", "250")
    monkeypatch.setenv("QUOTEZ_SYMBOLS", "synth_fx_alpha,SYNTH_FX_BETA")
    monkeypatch.setenv("QUOTEZ_LOG_LEVEL", "warning")
    config = ServerConfig.from_env()
    assert config.max_bars == 250
    assert config.symbols == ("SYNTH_FX_ALPHA", "SYNTH_FX_BETA")
    assert config.log_level == "WARNING"


def test_a_bad_environment_value_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUOTEZ_MAX_BARS", "many")
    with pytest.raises(InvalidRequest, match="whole number"):
        ServerConfig.from_env()


def test_flags_override_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUOTEZ_MAX_BARS", "250")
    monkeypatch.setenv("QUOTEZ_SOURCE", "mt5")
    assert ServerConfig.from_args([]).max_bars == 250
    assert ServerConfig.from_args([]).source == "mt5"
    overridden = ServerConfig.from_args(["--max-bars", "77", "--source", "replay"])
    assert overridden.max_bars == 77
    assert overridden.source == "replay"


def test_an_overridden_flag_survives_a_bad_environment_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Building the parser's defaults through ServerConfig would make this combination fatal
    # even though the caller replaced the offending value on the command line.
    monkeypatch.setenv("QUOTEZ_MAX_BARS", "999999")
    assert ServerConfig.from_args(["--max-bars", "100"]).max_bars == 100


def test_an_illegal_flag_value_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUOTEZ_MAX_BARS", raising=False)
    with pytest.raises(SystemExit) as caught:
        ServerConfig.from_args(["--max-bars", str(MAX_BARS_CEILING + 1)])
    assert caught.value.code == 2


# --------------------------------------------------------------------------------------
# The whitelist, through a client


@pytest.mark.anyio
async def test_the_whitelist_filters_list_symbols() -> None:
    async with connected(ServerConfig(symbols=(SYMBOL,))) as client:
        result = await client.call_tool("list_symbols", {})
        payload = result.structured_content
        assert payload is not None
        assert [entry["name"] for entry in payload["symbols"]] == [SYMBOL]
        assert payload["count"] == 1


@pytest.mark.anyio
async def test_the_whitelist_filters_the_resource_too() -> None:
    async with connected(ServerConfig(symbols=(SYMBOL,))) as client:
        contents = (await client.read_resource("symbols://list")).contents
        payload = json.loads(contents[0].text)
    assert [entry["name"] for entry in payload["symbols"]] == [SYMBOL]


@pytest.mark.anyio
async def test_the_whitelist_blocks_a_getter_without_admitting_the_symbol_exists() -> None:
    async with connected(ServerConfig(symbols=(SYMBOL,))) as client:
        blocked = await client.call_tool("get_quote", {"symbol": BLOCKED})
        invented = await client.call_tool("get_quote", {"symbol": "NO_SUCH_THING"})

    assert blocked.is_error is True
    blocked_text = blocked.content[0].text
    assert BLOCKED in blocked_text
    # Identical wording for "hidden" and "does not exist", so the error cannot be used to
    # enumerate what the operator chose not to expose.
    assert blocked_text.replace(BLOCKED, "X") == invented.content[0].text.replace(
        "NO_SUCH_THING", "X"
    )
    assert "block" not in blocked_text.lower()
    assert "permit" not in blocked_text.lower()


@pytest.mark.anyio
async def test_an_empty_whitelist_exposes_every_bundled_symbol(client: Client) -> None:
    payload = (await client.call_tool("list_symbols", {})).structured_content
    assert payload is not None
    assert [entry["name"] for entry in payload["symbols"]] == list(REPLAY_SYMBOLS)


# --------------------------------------------------------------------------------------
# max_bars, enforced on both bar tools


@pytest.mark.anyio
async def test_count_above_the_configured_cap_is_an_actionable_tool_error() -> None:
    async with connected(ServerConfig(max_bars=10)) as client:
        result = await client.call_tool(
            "get_bars", {"symbol": SYMBOL, "timeframe": "M1", "count": 50}
        )
    assert result.is_error is True
    text = result.content[0].text
    assert "10" in text
    assert "coarser timeframe" in text


@pytest.mark.anyio
async def test_count_above_the_schema_bound_is_rejected_before_the_handler_runs(
    client: Client,
) -> None:
    result = await client.call_tool(
        "get_bars", {"symbol": SYMBOL, "timeframe": "M1", "count": MAX_BARS_CEILING + 1}
    )
    assert result.is_error is True
    # The pydantic validation error proves the SDK rejected the call against the generated
    # input schema, so the handler was never entered.
    assert "less than or equal to" in result.content[0].text
    assert str(MAX_BARS_CEILING) in result.content[0].text


@pytest.mark.anyio
async def test_a_range_spanning_more_than_max_bars_is_rejected_before_fetching() -> None:
    async with connected(ServerConfig(max_bars=100)) as client:
        result = await client.call_tool(
            "get_bars_range",
            {
                "symbol": SYMBOL,
                "timeframe": "M1",
                "start": "2026-06-01T08:00:00Z",
                "end": "2026-06-12T14:00:00Z",
            },
        )
    assert result.is_error is True
    text = result.content[0].text
    assert "100" in text
    assert "Narrow the range or use a coarser timeframe" in text


@pytest.mark.anyio
async def test_the_same_range_is_served_at_a_coarser_timeframe() -> None:
    # The error above is actionable only if taking its advice actually works.
    async with connected(ServerConfig(max_bars=100)) as client:
        result = await client.call_tool(
            "get_bars_range",
            {
                "symbol": SYMBOL,
                "timeframe": "H4",
                "start": "2026-06-01T08:00:00Z",
                "end": "2026-06-12T14:00:00Z",
            },
        )
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["count"] == 19


@pytest.mark.anyio
async def test_the_account_login_is_masked_to_the_last_four_digits(client: Client) -> None:
    payload = (await client.call_tool("get_account", {})).structured_content
    assert payload is not None
    assert payload["login_masked"] == "****0000"
    assert payload["synthetic"] is True
    assert len(payload["login_masked"]) == 8
