# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-08-18

First public release. Read only, stdio only.

### Added

- MCP server built on `mcp>=2.0.0,<3` and `MCPServer`, served over stdio by the `quotez` console
  script or by `mcp run src/quotez/server.py` through the module level `mcp` global.
- Eight tools in a fixed registration order: `list_symbols`, `get_quote`, `get_bars`,
  `get_bars_range`, `symbol_info`, `get_account`, `list_positions`, `list_orders`. Each one is
  annotated `read_only_hint=True, open_world_hint=False` and returns a Pydantic model, so the SDK
  derives the output schema and validates every payload before it leaves the server.
- One concrete resource, `symbols://list`, serving the instrument universe as
  `application/json`.
- `MarketDataSource`, the protocol both sources implement, so nothing above it imports
  MetaTrader5.
- `ReplaySource`, backed by four generated instruments bundled in the package and read through
  `importlib.resources`. It reads no clock: `get_quote` derives from the last stored bar, which is
  what makes the README transcript reproducible.
- `Mt5Source`, reading a live terminal through read only calls only. The extension is imported
  lazily, the connection is opened once in the server lifespan, and every timestamp is built UTC
  aware.
- `quotez.aggregate`, rolling M1 bars up to M5, M15, M30, H1, H4 and D1 by wall clock buckets,
  dropping incomplete trailing buckets and clearing `spread` on aggregated bars. It is the replay
  source's roll up; `Mt5Source` asks the terminal for the timeframe instead, and the two therefore
  differ on D1 and H4 alignment and on `spread`, as Limitations records.
- `quotez.groups`, implementing MetaTrader's `symbols_get` group filter syntax for sources that
  have no terminal.
- Configuration through flags or `QUOTEZ_*` variables: source, symbol whitelist, bar cap
  (default 1000, ceiling 5000) and log level, all validated at startup.
- Errors split across the two MCP channels: `SymbolNotFound` and `InvalidRequest` as ordinary
  exceptions the model can retry from, `SourceUnavailable` as an `MCPError` protocol error.
- `examples/agent_session.py`, which prints the README transcript from an in-process client, and a
  test asserting the README block matches it byte for byte.
- Continuous integration on Ubuntu 3.11 to 3.13, macOS 3.13, a Windows 3.12 job exercising the
  `mt5` extra, a macOS job proving that extra is a no-op off Windows, and an advisory 3.14 job.

[0.1.0]: https://github.com/PNX89/QUOTEZ/releases/tag/v0.1.0
