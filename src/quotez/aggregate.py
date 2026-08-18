"""Rolling M1 bars up to a coarser timeframe.

A source stores exactly one base timeframe, M1, and every other timeframe is built here.
One stored copy of the data, one roll-up, and the roll-up is testable on its own, which
matters because its failure mode is silent: a wrong aggregation returns plausible numbers
forever and nothing ever raises.

Buckets are wall-clock, computed by floor division on the epoch second, never by grouping
every N rows. Positional grouping agrees with wall-clock bucketing on a gapless series and
disagrees the moment the series has a hole in it, and a market series is nothing but
holes: every overnight break and every weekend is one. Grouping 360 bar sessions in fours
puts Friday's close and Monday's open in the same bar and reports it as a 4 hour candle.

An incomplete trailing bucket is dropped rather than emitted as a partial bar. A bucket is
emitted only when the input holds a bar at or after that bucket's end, which drops the
newest bucket of the dataset (still forming, or simply not downloaded yet) while keeping a
bucket that a session gap left half filled. The session ending is not the same event as
the data running out, and only the second one makes a bar untrustworthy.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from quotez.errors import InvalidRequest
from quotez.models import Bar

__all__ = [
    "BASE_SECONDS",
    "BASE_TIMEFRAME",
    "TIMEFRAME_SECONDS",
    "aggregate",
    "aggregate_seconds",
    "bucket_start",
    "timeframe_seconds",
]

TIMEFRAME_SECONDS: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}

BASE_TIMEFRAME = "M1"
BASE_SECONDS = TIMEFRAME_SECONDS[BASE_TIMEFRAME]


def timeframe_seconds(target: str) -> int:
    """Seconds in one bar of `target`, or `InvalidRequest` naming the supported set."""
    try:
        return TIMEFRAME_SECONDS[target]
    except KeyError:
        supported = ", ".join(TIMEFRAME_SECONDS)
        raise InvalidRequest(
            f"Unknown timeframe {target!r}. Supported timeframes: {supported}."
        ) from None


def bucket_start(ts: datetime, seconds: int) -> datetime:
    """The UTC left edge of the `seconds` wide bucket containing `ts`.

    `ts` must be timezone aware. A naive datetime is rejected rather than assumed to be
    UTC, because `datetime.timestamp()` resolves a naive value against the LOCAL zone and
    would quietly shift every bucket by the machine's offset.
    """
    if ts.tzinfo is None:
        raise InvalidRequest(
            f"Timestamp {ts.isoformat()} has no timezone. Pass UTC times, for example "
            "2026-06-01T08:00:00Z."
        )
    if seconds <= 0:
        raise InvalidRequest(f"Bucket width must be positive, got {seconds} seconds.")
    edge = int(ts.timestamp() // seconds) * seconds
    return datetime.fromtimestamp(edge, tz=UTC)


def aggregate_seconds(bars: Sequence[Bar], seconds: int) -> list[Bar]:
    """Roll M1 `bars` up into `seconds` wide bars. See the module docstring for the rules.

    `bars` must be M1 and strictly ascending in time, oldest first, which is how every
    source returns them.
    """
    if seconds <= 0 or seconds % BASE_SECONDS != 0:
        raise InvalidRequest(
            f"Aggregation period must be a positive whole number of {BASE_SECONDS} second "
            f"buckets, got {seconds} seconds."
        )
    if not bars:
        return []
    _require_ascending(bars)
    if seconds == BASE_SECONDS:
        # Identity, and deliberately not routed through the completeness rule: a bucket
        # holding exactly one base bar is complete by definition, so nothing is dropped.
        return list(bars)

    buckets: dict[datetime, list[Bar]] = {}
    for candle in bars:
        buckets.setdefault(bucket_start(candle.time, seconds), []).append(candle)

    width = timedelta(seconds=seconds)
    newest = bars[-1].time
    # Insertion order is ascending because the input is, so the result needs no sort.
    return [_combine(start, group) for start, group in buckets.items() if start + width <= newest]


def aggregate(bars: Sequence[Bar], target: str) -> list[Bar]:
    """Roll M1 `bars` up to the `target` timeframe, oldest first."""
    return aggregate_seconds(bars, timeframe_seconds(target))


def _require_ascending(bars: Sequence[Bar]) -> None:
    for previous, current in pairwise(bars):
        if current.time <= previous.time:
            raise InvalidRequest(
                "Bars must be strictly ascending in time, oldest first, but "
                f"{current.time.isoformat()} follows {previous.time.isoformat()}."
            )


def _combine(start: datetime, group: list[Bar]) -> Bar:
    return Bar(
        time=start,
        open=group[0].open,
        high=max(candle.high for candle in group),
        low=min(candle.low for candle in group),
        close=group[-1].close,
        tick_volume=sum(candle.tick_volume for candle in group),
        # A spread is a point in time property of a quote, not a flow like volume.
        # Summing it would report a 4 hour bar with a 480 point spread.
        spread=None,
    )
