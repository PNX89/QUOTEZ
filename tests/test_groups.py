"""Tests for quotez.groups.

The filter has to agree with what the MetaTrader terminal does, including the part people
get wrong: conditions apply in order, so an inclusion written after an exclusion undoes
it. That is the documented behaviour and it is pinned here rather than corrected.
"""

from __future__ import annotations

import pytest

from quotez.errors import InvalidRequest
from quotez.groups import match_group

UNIVERSE = ["SYNTH_FX_ALPHA", "SYNTH_FX_BETA", "SYNTH_IDX_GAMMA", "SYNTH_MTL_DELTA"]


def selected(group: str | None) -> list[str]:
    return [name for name in UNIVERSE if match_group(name, group)]


def test_none_returns_every_symbol() -> None:
    assert selected(None) == UNIVERSE


def test_leading_wildcard_matches_a_suffix() -> None:
    assert selected("*ALPHA") == ["SYNTH_FX_ALPHA"]
    assert selected("*A") == UNIVERSE
    assert selected("*ZULU") == []


def test_trailing_wildcard_matches_a_prefix() -> None:
    assert selected("SYNTH_FX*") == ["SYNTH_FX_ALPHA", "SYNTH_FX_BETA"]
    assert selected("OTHER*") == []


def test_negation_excludes() -> None:
    assert selected("*, !*FX*") == ["SYNTH_IDX_GAMMA", "SYNTH_MTL_DELTA"]
    assert selected("*, !*FX*, !*MTL*") == ["SYNTH_IDX_GAMMA"]


def test_inclusions_must_come_before_exclusions() -> None:
    # The terminal applies conditions in order, so a trailing "*" puts back everything the
    # exclusion just removed. Both results below are correct; only the first is useful.
    assert selected("*, !*FX*") == ["SYNTH_IDX_GAMMA", "SYNTH_MTL_DELTA"]
    assert selected("!*FX*, *") == UNIVERSE


def test_a_pattern_without_a_wildcard_is_an_exact_match() -> None:
    assert selected("SYNTH_FX_BETA") == ["SYNTH_FX_BETA"]
    assert selected("SYNTH_FX") == []


def test_matching_is_case_sensitive() -> None:
    assert match_group("SYNTH_FX_ALPHA", "*fx*") is False


def test_bracketed_broker_symbols_match_literally() -> None:
    # A glob library would read [SP500] as a character class and match the single
    # character "S", so the matcher here honours "*" and nothing else.
    assert match_group("[SP500]", "[SP500]") is True
    assert match_group("S", "[SP500]") is False
    assert match_group("[SP500].cash", "[SP500]*") is True


def test_an_empty_condition_is_rejected() -> None:
    for group in ("", "*,", "*, !", "  "):
        with pytest.raises(InvalidRequest, match="empty condition"):
            match_group("SYNTH_FX_ALPHA", group)
