"""Packaging hygiene: the things that only break once the package leaves the source tree."""

from __future__ import annotations

import sys
import tomllib
from importlib import metadata, resources
from pathlib import Path

from packaging.requirements import Requirement

import quotez

REPO = Path(__file__).resolve().parent.parent
MANIFEST = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
WORKFLOW = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def test_version_matches_the_installed_distribution() -> None:
    assert quotez.__version__ == metadata.version("quotez")


def test_py_typed_marker_ships_with_the_package() -> None:
    # Without this file in the wheel, every type hint in the package is invisible to a
    # consumer's type checker.
    assert resources.files("quotez").joinpath("py.typed").is_file()


def test_the_typed_classifier_is_backed_by_a_type_checker_that_actually_runs() -> None:
    """Shipping `py.typed` with nothing checking the hints is a claim with no test behind it.

    The marker and the classifier tell a consumer's checker to trust every annotation in
    this package. That is worth failing a build over, so the checker is configured here, run
    in continuous integration, and asserted from both ends by this test.
    """
    assert "Typing :: Typed" in MANIFEST["project"]["classifiers"]
    dev = MANIFEST["dependency-groups"]["dev"]
    assert any(requirement.startswith("mypy") for requirement in dev)
    settings = MANIFEST["tool"]["mypy"]
    assert settings["strict"] is True
    assert settings["files"] == ["src"]
    assert "uv run mypy" in WORKFLOW


def test_every_import_the_test_suite_makes_is_a_declared_dependency() -> None:
    # `packaging` used to be imported here and declared nowhere, resolving only because
    # pytest happens to depend on it. A pytest release that drops it would have broken this
    # suite for a reason with nothing to do with the code.
    declared = {Requirement(raw).name.lower() for raw in MANIFEST["dependency-groups"]["dev"]}
    assert {"packaging", "pytest", "mypy", "ruff", "anyio", "mcp"} <= declared


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
