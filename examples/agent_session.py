"""Print the agent session the README embeds. Regenerate with:

    uv run python examples/agent_session.py

An MCP client is connected to a QUOTEZ server in the same process, five tools are called,
and what comes back is printed. The last call is a deliberate mistake, because how a tool
reports failure is the half of a tool contract that transcripts usually leave out.

The output is byte stable. The server is built from an explicit `ServerConfig`, so nothing
reads the environment; the replay source derives its quote from the last stored bar, so
nothing reads a clock; and the tool list is a registration order rather than a set. That is
what lets `tests/test_readme.py` assert the README block against this script's stdout
instead of trusting a paste.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
from mcp import Client

from quotez.config import ServerConfig
from quotez.server import build_server

SYMBOL = "SYNTH_FX_ALPHA"


def render(payload: Any) -> str:
    """A tool's structured content as JSON, one nested object per line.

    `json.dumps(indent=2)` is correct but spends fifty lines on five bars. Every payload
    these tools return is a flat object, optionally holding one list of flat objects, so
    collapsing that one level is enough and loses nothing.
    """
    if not isinstance(payload, dict):
        return json.dumps(payload)
    fields = []
    for key, value in payload.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            rows = ",\n".join("    " + json.dumps(item) for item in value)
            fields.append(f"  {json.dumps(key)}: [\n{rows}\n  ]")
        else:
            fields.append(f"  {json.dumps(key)}: {json.dumps(value)}")
    return "{\n" + ",\n".join(fields) + "\n}"


def call_line(name: str, arguments: dict[str, Any]) -> str:
    """The call as the client sent it. Values are JSON, because that is what went on the wire."""
    shown = ", ".join(f"{key}={json.dumps(value)}" for key, value in arguments.items())
    return f">>> {name}({shown})"


async def main() -> None:
    server = build_server(ServerConfig(source="replay"))
    async with Client(server) as client:
        print("QUOTEZ over an in-memory MCP client, source=replay.")
        print("Every price below is generated. This repository bundles no real market data.")

        for name, arguments in (
            ("list_symbols", {"group": "*FX*"}),
            ("get_quote", {"symbol": SYMBOL}),
            ("get_bars", {"symbol": SYMBOL, "timeframe": "H1", "count": 5}),
            ("symbol_info", {"symbol": SYMBOL}),
        ):
            result = await client.call_tool(name, arguments)
            print()
            print(call_line(name, arguments))
            print(render(result.structured_content))

        print()
        print("A symbol that does not exist, to show what the model actually sees:")
        arguments = {"symbol": "NOT_A_SYMBOL"}
        result = await client.call_tool("get_quote", arguments)
        print()
        print(call_line("get_quote", arguments))
        print(f"is_error: {str(result.is_error).lower()}")
        for block in result.content:
            print(block.text)


if __name__ == "__main__":
    anyio.run(main)
