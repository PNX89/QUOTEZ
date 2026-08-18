"""Tests for quotez.models.

The models are the tools' output schemas, so two properties are worth pinning: every
field explains itself to the model that reads the schema, and nothing that carries prices
or money can be built without saying where it came from and whether it is real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, ValidationError

from quotez import models
from quotez.models import Account, Bar, BarSeries, Quote

ALL_MODELS = [
    value
    for value in vars(models).values()
    if isinstance(value, type) and issubclass(value, BaseModel) and value is not BaseModel
]

LABELLED_MODELS = [model for model in ALL_MODELS if "synthetic" in model.model_fields]


def test_the_model_set_is_the_expected_one() -> None:
    assert {model.__name__ for model in ALL_MODELS} == {
        "Account",
        "Bar",
        "BarSeries",
        "Order",
        "OrderList",
        "Position",
        "PositionList",
        "Quote",
        "SymbolList",
        "SymbolSpec",
        "SymbolSummary",
    }


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda model: model.__name__)
def test_every_field_describes_itself(model: type[BaseModel]) -> None:
    # The descriptions are not decoration: they are the schema text the model reads
    # before deciding what to call and how to read the answer.
    missing = [name for name, field in model.model_fields.items() if not field.description]
    assert missing == []


@pytest.mark.parametrize("model", LABELLED_MODELS, ids=lambda model: model.__name__)
def test_source_and_synthetic_are_required(model: type[BaseModel]) -> None:
    assert model.model_fields["source"].is_required()
    assert model.model_fields["synthetic"].is_required()


def test_every_payload_model_is_labelled() -> None:
    # Bar and Position are nested inside a labelled container, so the label is carried
    # once per payload rather than once per row.
    unlabelled = {model.__name__ for model in ALL_MODELS} - {
        model.__name__ for model in LABELLED_MODELS
    }
    assert unlabelled == {"Bar", "Order", "Position", "SymbolSummary"}


def test_a_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError, match="UTC offset"):
        Bar(
            time=datetime(2026, 6, 1, 8, 0),
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            tick_volume=1,
            spread=2,
        )


def test_an_offset_timestamp_is_normalised_to_utc() -> None:
    quote = Quote(
        symbol="SYNTH_FX_ALPHA",
        time=datetime(2026, 6, 1, 15, 30, tzinfo=timezone(timedelta(hours=2))),
        bid=1.0,
        ask=1.1,
        spread_points=10,
        source="replay",
        synthetic=True,
    )
    assert quote.time == datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
    assert quote.time.tzinfo is UTC


def test_utc_times_serialise_with_a_z_suffix() -> None:
    # The README transcript is asserted byte for byte, so the wire format of a timestamp
    # has to be stable and unambiguous.
    series = BarSeries(
        symbol="SYNTH_FX_ALPHA",
        timeframe="H1",
        source="replay",
        synthetic=True,
        count=1,
        bars=[
            Bar(
                time=datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
                open=1.0,
                high=2.0,
                low=0.5,
                close=1.5,
                tick_volume=10,
                spread=None,
            )
        ],
    )
    assert '"time":"2026-06-01T08:00:00Z"' in series.model_dump_json()


def test_an_account_cannot_omit_the_synthetic_flag() -> None:
    with pytest.raises(ValidationError):
        Account(
            login_masked="****0000",
            currency="SYN",
            balance=100000.0,
            equity=100000.0,
            margin=0.0,
            margin_free=100000.0,
            margin_level=0.0,
            leverage=100,
            source="replay",
        )
