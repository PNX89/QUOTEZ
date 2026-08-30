"""Server configuration, from command line flags or from the environment.

Two entry paths need configuring and they do not agree on how. The console script takes
flags. `mcp run src/quotez/server.py` imports the module and forwards nothing, so it can
only be configured through `QUOTEZ_*` variables. `from_args` therefore reads the
environment for its defaults and lets a flag override it, which keeps one precedence rule
instead of two code paths.

Everything is validated at construction, so an illegal value fails at startup with a
message naming the bound rather than at the first tool call. `max_bars` above the ceiling
is rejected rather than clamped: silently serving 5000 bars to a caller who asked for
50000 is the kind of accommodation that turns into a support question later.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from quotez import __version__
from quotez.errors import InvalidRequest

__all__ = [
    "LOG_LEVELS",
    "MAX_BARS_CEILING",
    "MAX_BARS_DEFAULT",
    "SOURCE_NAMES",
    "ServerConfig",
    "build_parser",
    "parse_symbols",
]

MAX_BARS_DEFAULT = 1000
MAX_BARS_CEILING = 5000

SOURCE_NAMES = ("replay", "mt5")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

ENV_SOURCE = "QUOTEZ_SOURCE"
ENV_SYMBOLS = "QUOTEZ_SYMBOLS"
ENV_MAX_BARS = "QUOTEZ_MAX_BARS"
ENV_LOG_LEVEL = "QUOTEZ_LOG_LEVEL"


@dataclass(frozen=True)
class ServerConfig:
    """What the server was told to do, validated.

    `symbols` is the whitelist, held uppercased so matching is case insensitive. An empty
    tuple means every symbol the source has: denying everything until an operator
    configures something would make the quickstart return an empty list, which is the wrong
    first impression for a repository whose point is that it runs immediately.
    """

    source: Literal["replay", "mt5"] = "replay"
    symbols: tuple[str, ...] = ()
    max_bars: int = MAX_BARS_DEFAULT
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.source not in SOURCE_NAMES:
            raise InvalidRequest(
                f"Unknown source {self.source!r}. Choose one of: {', '.join(SOURCE_NAMES)}."
            )
        if self.log_level not in LOG_LEVELS:
            raise InvalidRequest(
                f"Unknown log level {self.log_level!r}. Choose one of: {', '.join(LOG_LEVELS)}."
            )
        if self.max_bars < 1 or self.max_bars > MAX_BARS_CEILING:
            raise InvalidRequest(
                f"max_bars must be between 1 and {MAX_BARS_CEILING}, got {self.max_bars}. "
                "Request a coarser timeframe rather than a larger window."
            )
        object.__setattr__(self, "symbols", tuple(name.upper() for name in self.symbols))

    def allows(self, symbol: str) -> bool:
        """True when `symbol` is inside the whitelist, or when there is no whitelist."""
        return not self.symbols or symbol.upper() in self.symbols

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> ServerConfig:
        """Build from `QUOTEZ_*` variables, falling back to the defaults."""
        env = os.environ if environ is None else environ
        return cls(
            # Lowercased for the same reason log_level is uppercased three lines below: the
            # console script's --source already normalises case through argparse's
            # type=str.lower, and QUOTEZ_SOURCE is meant to configure both entry points the
            # same way. Left raw, a value that works as a flag would crash as an env var.
            source=env.get(ENV_SOURCE, "replay").lower(),  # type: ignore[arg-type]
            symbols=parse_symbols(env.get(ENV_SYMBOLS)),
            max_bars=_parse_max_bars(env.get(ENV_MAX_BARS)),
            log_level=env.get(ENV_LOG_LEVEL, "INFO").upper(),
        )

    @classmethod
    def from_args(cls, argv: Sequence[str] | None = None) -> ServerConfig:
        """Build from command line flags, defaulting to whatever the environment says.

        An illegal value exits 2 through `argparse`, which prints usage to stderr. That
        matters on stdio: usage output would corrupt the wire if it reached stdout, and it
        happens before serving starts either way.
        """
        parser = build_parser()
        args = parser.parse_args(argv)
        try:
            return cls(
                source=args.source,
                symbols=parse_symbols(args.symbols),
                max_bars=args.max_bars,
                log_level=args.log_level,
            )
        except InvalidRequest as failure:
            parser.error(str(failure))


def build_parser() -> argparse.ArgumentParser:
    """The `quotez` command line. Defaults are the raw environment values.

    Defaults are deliberately left unvalidated here. Building them through `ServerConfig`
    would make a bad `QUOTEZ_MAX_BARS` fatal even when the caller passed `--max-bars` to
    override it. Flag values are checked by `argparse`, and everything else by
    `ServerConfig.__post_init__`.
    """
    parser = argparse.ArgumentParser(
        prog="quotez",
        description="Serve MetaTrader 5 market data to an MCP client over stdio. Read only.",
    )
    parser.add_argument(
        "--source",
        type=str.lower,
        choices=SOURCE_NAMES,
        default=os.environ.get(ENV_SOURCE, "replay"),
        help="Where market data comes from. 'replay' uses the bundled generated files and "
        "needs nothing installed. 'mt5' needs Windows and a running MetaTrader 5 terminal.",
    )
    parser.add_argument(
        "--symbols",
        default=os.environ.get(ENV_SYMBOLS, ""),
        metavar="A,B,C",
        help="Comma separated whitelist of symbols to expose, case insensitive. "
        "Empty exposes everything the source has.",
    )
    parser.add_argument(
        "--max-bars",
        type=int,
        # Stripped before the fallback applies, unlike --symbols and --source above: argparse
        # runs `type=int` on a string default too, and int("") raises. _parse_max_bars treats
        # a blank QUOTEZ_MAX_BARS as "use the default", so this entry point has to reach the
        # same default rather than fail on a value from_env accepts.
        default=os.environ.get(ENV_MAX_BARS, "").strip() or str(MAX_BARS_DEFAULT),
        metavar="N",
        help=f"Most bars one call may return, 1 to {MAX_BARS_CEILING} "
        f"(default {MAX_BARS_DEFAULT}).",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=LOG_LEVELS,
        default=os.environ.get(ENV_LOG_LEVEL, "INFO"),
        help="Logging threshold. Records always go to stderr, because stdout is the wire.",
    )
    parser.add_argument("--version", action="version", version=f"quotez {__version__}")
    return parser


def parse_symbols(raw: str | None) -> tuple[str, ...]:
    """Split a comma separated whitelist. Blank entries are dropped, not treated as empty."""
    if not raw:
        return ()
    return tuple(name.strip().upper() for name in raw.split(",") if name.strip())


def _parse_max_bars(raw: str | None) -> int:
    if raw is None or not raw.strip():
        return MAX_BARS_DEFAULT
    try:
        return int(raw)
    except ValueError:
        raise InvalidRequest(f"{ENV_MAX_BARS} must be a whole number, got {raw!r}.") from None
