"""Generate the bundled replay fixtures. Run once by hand; commit the output.

    python scripts/generate_replay_data.py

No real market data is bundled with this repository, and that is a licensing decision
rather than a preference. MetaTrader exports are the broker's licensed feed and, for index
and equity CFDs, sit on an exchange licence underneath that. Yahoo Finance forbids
redistributing anything it serves. HistData grants no redistribution right anywhere in its
terms, and silence is not a licence. Committing any of those to an MIT repository would
relicense somebody else's data.

So the four instruments here are invented, and named so that nobody can mistake them for
quotes: SYNTH_FX_ALPHA, SYNTH_FX_BETA, SYNTH_IDX_GAMMA, SYNTH_MTL_DELTA. The FX, IDX and
MTL infixes exist so the group filter has something to demonstrate.

Two properties matter more than realism.

Determinism: one `random.Random(20260818)`, drawn in a fixed symbol order, stdlib only. The
same command produces byte-identical CSVs on any machine, which is what lets the README
transcript be asserted byte for byte. This script is never run at import time or test time.

Gaps: bars cover 08:00 to 14:00 UTC on weekdays only, from Monday 2026-06-01 to Friday
2026-06-12. That is 360 bars a day over 10 trading days, 3600 per symbol, with 9 breaks in
the series: 8 overnight and 1 across the weekend in the middle. Aggregation is only
interesting over holes, and a gapless series would let positional grouping pass every test
that wall-clock bucketing passes.
"""

from __future__ import annotations

import csv
import json
import math
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

SEED = 20260818
DATA_DIR = Path(__file__).resolve().parent.parent / "src" / "quotez" / "data"

FIRST_DAY = date(2026, 6, 1)
LAST_DAY = date(2026, 6, 12)
SESSION_OPEN = 8
SESSION_CLOSE = 14
SESSION_MINUTES = (SESSION_CLOSE - SESSION_OPEN) * 60

# Sub-moves walked inside each minute. The bar's high and low are the extremes of that
# walk, which is what makes high >= max(open, close) and low <= min(open, close) true by
# construction rather than by a repair pass afterwards.
TICKS_PER_BAR = 12

# A session opens away from the previous close, so the series is discontinuous in price as
# well as in time. Scored as this many minutes of variance per calendar day of closure,
# which puts the weekend gap at roughly the size of two overnight gaps.
GAP_MINUTES_PER_DAY = 120


@dataclass(frozen=True)
class Instrument:
    name: str
    description: str
    digits: int
    start_price: float
    annual_drift: float
    annual_volatility: float
    base_spread_points: int
    spread_jitter_points: int
    typical_ticks: int
    trade_stops_level: int
    trade_contract_size: float
    volume_max: float
    currency_base: str

    @property
    def point(self) -> float:
        return round(10.0**-self.digits, self.digits)


INSTRUMENTS = (
    Instrument(
        name="SYNTH_FX_ALPHA",
        description="Synthetic FX pair Alpha",
        digits=5,
        start_price=1.10000,
        annual_drift=0.02,
        annual_volatility=0.07,
        base_spread_points=8,
        spread_jitter_points=4,
        typical_ticks=45,
        trade_stops_level=10,
        trade_contract_size=100_000.0,
        volume_max=100.0,
        currency_base="SYA",
    ),
    Instrument(
        name="SYNTH_FX_BETA",
        description="Synthetic FX pair Beta",
        digits=3,
        start_price=145.000,
        annual_drift=-0.04,
        annual_volatility=0.10,
        base_spread_points=12,
        spread_jitter_points=6,
        typical_ticks=38,
        trade_stops_level=20,
        trade_contract_size=100_000.0,
        volume_max=100.0,
        currency_base="SYB",
    ),
    Instrument(
        name="SYNTH_IDX_GAMMA",
        description="Synthetic equity index Gamma",
        digits=2,
        start_price=4500.00,
        annual_drift=0.08,
        annual_volatility=0.18,
        base_spread_points=50,
        spread_jitter_points=30,
        typical_ticks=60,
        trade_stops_level=100,
        trade_contract_size=1.0,
        volume_max=50.0,
        currency_base="SYG",
    ),
    Instrument(
        name="SYNTH_MTL_DELTA",
        description="Synthetic precious metal Delta",
        digits=2,
        start_price=2050.00,
        annual_drift=0.05,
        annual_volatility=0.15,
        base_spread_points=30,
        spread_jitter_points=18,
        typical_ticks=25,
        trade_stops_level=50,
        trade_contract_size=100.0,
        volume_max=50.0,
        currency_base="SYD",
    ),
)

# 252 trading days of 6 hours each, expressed in minutes, so an annual volatility can be
# scaled down to the per-minute step of a geometric walk.
MINUTES_PER_YEAR = 252 * SESSION_MINUTES


def trading_days() -> list[date]:
    days: list[date] = []
    day = FIRST_DAY
    while day <= LAST_DAY:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def session_minutes(day: date) -> list[datetime]:
    opening = datetime(day.year, day.month, day.day, SESSION_OPEN, tzinfo=UTC)
    return [opening + timedelta(minutes=i) for i in range(SESSION_MINUTES)]


def volume_profile(minute_of_session: int) -> float:
    """The familiar U shape: busy at the open, quiet at lunch, busy into the close."""
    fraction = minute_of_session / SESSION_MINUTES
    return 0.55 + 1.45 * (2.0 * fraction - 1.0) ** 2


def spread_profile(minute_of_session: int) -> float:
    """Spreads are widest in the first minutes of the session and settle after that."""
    return 1.0 + 2.2 * math.exp(-minute_of_session / 12.0)


def generate_bars(rng: random.Random, instrument: Instrument) -> list[dict[str, object]]:
    drift = instrument.annual_drift / MINUTES_PER_YEAR
    volatility = instrument.annual_volatility / math.sqrt(MINUTES_PER_YEAR)
    tick_volatility = volatility / math.sqrt(TICKS_PER_BAR)

    def quoted(value: float) -> str:
        # Prices are written at the instrument's own precision, so the file reads the way a
        # 5 digit FX feed reads. Rounding is monotonic, which is why rounding the walk's
        # extremes independently cannot invert high >= max(open, close) or
        # low <= min(open, close).
        return f"{value:.{instrument.digits}f}"

    price = instrument.start_price
    rows: list[dict[str, object]] = []
    previous_day: date | None = None

    for day in trading_days():
        if previous_day is not None:
            closed_days = (day - previous_day).days
            gap_sigma = volatility * math.sqrt(GAP_MINUTES_PER_DAY * closed_days)
            price *= math.exp(-0.5 * gap_sigma**2 + gap_sigma * rng.gauss(0.0, 1.0))
        previous_day = day

        for minute_of_session, stamp in enumerate(session_minutes(day)):
            opening = price
            path = [opening]
            for _ in range(TICKS_PER_BAR):
                step = drift / TICKS_PER_BAR - 0.5 * tick_volatility**2
                path.append(path[-1] * math.exp(step + tick_volatility * rng.gauss(0.0, 1.0)))
            price = path[-1]

            ticks = max(1, round(instrument.typical_ticks * volume_profile(minute_of_session)))
            spread = instrument.base_spread_points + rng.randint(0, instrument.spread_jitter_points)
            rows.append(
                {
                    "time": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "open": quoted(opening),
                    "high": quoted(max(path)),
                    "low": quoted(min(path)),
                    "close": quoted(price),
                    "tick_volume": rng.randint(max(1, ticks - 8), ticks + 8),
                    "spread": max(1, round(spread * spread_profile(minute_of_session))),
                }
            )
    return rows


def contract_spec(instrument: Instrument) -> dict[str, object]:
    """The static half of `symbol_info`.

    `spread` is deliberately absent: MetaTrader reports the CURRENT spread there, so the
    replay source reads it off the last stored bar instead of pinning a stale number here.
    `trade_tick_value` is exactly `trade_contract_size * trade_tick_size`, because these
    instruments settle in the account currency and there is no cross rate to convert
    through.
    """
    point = instrument.point
    return {
        "name": instrument.name,
        "description": instrument.description,
        "digits": instrument.digits,
        "point": point,
        "spread_float": True,
        "trade_stops_level": instrument.trade_stops_level,
        "trade_freeze_level": 0,
        "trade_tick_value": round(instrument.trade_contract_size * point, 6),
        "trade_tick_size": point,
        "trade_contract_size": instrument.trade_contract_size,
        "volume_min": 0.01,
        "volume_max": instrument.volume_max,
        "volume_step": 0.01,
        "currency_base": instrument.currency_base,
        "currency_profit": "SYN",
        "currency_margin": instrument.currency_base,
    }


def write_csv(instrument: Instrument, rows: list[dict[str, object]]) -> None:
    path = DATA_DIR / f"{instrument.name}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["time", "open", "high", "low", "close", "tick_volume", "spread"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    rng = random.Random(SEED)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    specs = []
    for instrument in INSTRUMENTS:
        write_csv(instrument, generate_bars(rng, instrument))
        specs.append(contract_spec(instrument))
    (DATA_DIR / "symbols.json").write_text(json.dumps(specs, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
