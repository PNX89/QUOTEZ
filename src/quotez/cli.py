"""The `quotez` console script.

Parse flags, point logging at stderr, build the server, serve stdio until the host closes
the connection.

Logging is configured before the server is built and it is configured explicitly, with
`stream=sys.stderr` and `force=True`. On stdio, stdout IS the JSON-RPC transport. The SDK
moves the wire to a private descriptor once serving starts and diverts flushed stdout
writes to stderr, but anything flushed BEFORE serving begins still lands on the wire and
corrupts the stream. A bare `logging.StreamHandler()` happens to default to stderr today,
and naming the stream here is what stops somebody from "fixing" that later.

`force=True` is not decoration either. Importing the SDK installs a handler on the root
logger, and `basicConfig` is a documented no-op once any handler exists, so without it this
call would silently do nothing and the destination of every log record would be somebody
else's choice.

Exit codes: 0 for a normal shutdown, 1 for a startup failure such as an unreachable
MetaTrader terminal, 2 for a usage or argument error, which is what argparse returns.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence

from quotez.config import ServerConfig
from quotez.errors import QuotezError
from quotez.server import build_server

__all__ = ["main"]

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

log = logging.getLogger("quotez")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the server. `--help` and `--version` exit through argparse before this returns."""
    config = ServerConfig.from_args(argv)
    logging.basicConfig(stream=sys.stderr, level=config.log_level, format=LOG_FORMAT, force=True)
    log.info(
        "Starting QUOTEZ: source=%s, max_bars=%d, symbols=%s",
        config.source,
        config.max_bars,
        ",".join(config.symbols) or "all",
    )
    try:
        # Blocks for the life of the server. Stdio is the default transport and takes no
        # argument; the lifespan opens the data source inside this call.
        build_server(config).run()
    except (KeyboardInterrupt, QuotezError, BaseExceptionGroup) as error:
        failure = _find(error, QuotezError)
        if failure is not None:
            log.error("%s", failure)
            return 1
        if _find(error, KeyboardInterrupt) is None:
            raise
        log.info("Interrupted, shutting down.")
    return 0


def _find(error: BaseException, kind: type[BaseException]) -> BaseException | None:
    """The first `kind` inside `error`, unwrapping the exception groups anyio raises.

    A lifespan failure does not arrive bare: anyio runs the lifespan in a task group and a
    task group reports through a group, so matching on the type alone would miss it.
    """
    if isinstance(error, kind):
        return error
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            found = _find(nested, kind)
            if found is not None:
                return found
    return None


if __name__ == "__main__":
    raise SystemExit(main())
