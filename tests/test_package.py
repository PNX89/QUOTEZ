"""Packaging hygiene: the things that only break once the package leaves the source tree."""

from __future__ import annotations

import sys
from importlib import metadata, resources

from packaging.requirements import Requirement

import quotez


def test_version_matches_the_installed_distribution() -> None:
    assert quotez.__version__ == metadata.version("quotez")


def test_py_typed_marker_ships_with_the_package() -> None:
    # Without this file in the wheel, every type hint in the package is invisible to a
    # consumer's type checker.
    assert resources.files("quotez").joinpath("py.typed").is_file()


def test_public_names_are_importable() -> None:
    missing = [name for name in quotez.__all__ if not hasattr(quotez, name)]
    assert missing == []


def test_the_mt5_extra_resolves_to_nothing_off_windows() -> None:
    """`pip install quotez[mt5]` must succeed as a no-op on macOS and Linux.

    MetaTrader5 publishes win_amd64 wheels and no source distribution, so without the
    environment marker the install fails at RESOLUTION with "no matching distribution
    found", before any lazy import could help.
    """
    requirements = [
        Requirement(raw)
        for raw in metadata.distribution("quotez").metadata.get_all("Requires-Dist") or []
    ]
    extra = [
        requirement
        for requirement in requirements
        if requirement.marker is not None
        and requirement.marker.evaluate({"extra": "mt5", "sys_platform": "win32"})
    ]
    assert [requirement.name.lower() for requirement in extra] == ["metatrader5"]
    for platform in ("darwin", "linux"):
        assert not extra[0].marker.evaluate({"extra": "mt5", "sys_platform": platform})
    # And on this machine, whichever it is, the answer agrees with sys.platform.
    installs_here = extra[0].marker.evaluate({"extra": "mt5"})
    assert installs_here is (sys.platform == "win32")


def test_the_bundled_data_ships_inside_the_package() -> None:
    # Hatchling only carries files under the declared package directory, and a wheel that
    # omits the CSVs breaks uvx while leaving the source checkout working.
    package = resources.files("quotez.data")
    assert sum(1 for entry in package.iterdir() if entry.name.endswith(".csv")) == 4
    assert package.joinpath("symbols.json").is_file()
