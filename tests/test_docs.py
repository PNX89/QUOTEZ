"""The documentation is asserted against the code, not proofread.

A README drifts silently: a tool gets renamed, a default changes, a flag is added, and the
prose keeps describing the previous version for months. Everything in here is a claim the
README or the changelog makes that the code can be asked about directly, so drift fails the
suite instead of surviving until a reviewer notices.

`tests/test_readme.py` covers the transcript itself. This module covers the rest of the
documentation: the tool table, the configuration surface, the citations, the changelog and
the workflow that has to exist for the badge at the top of the README to mean anything.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from quotez import __version__
from quotez.aggregate import TIMEFRAME_SECONDS
from quotez.config import (
    ENV_LOG_LEVEL,
    ENV_MAX_BARS,
    ENV_SOURCE,
    ENV_SYMBOLS,
    MAX_BARS_CEILING,
    MAX_BARS_DEFAULT,
    build_parser,
)
from tests.conftest import TOOL_NAMES

REPO = Path(__file__).resolve().parent.parent
README = (REPO / "README.md").read_text(encoding="utf-8")
CHANGELOG = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

# Claims this repository is not entitled to make. "read only by construction" is a statement
# about the code; "secure" and "guarantees" are statements about outcomes nobody can promise
# for a server whose annotations the client is free to ignore.
FORBIDDEN_CLAIMS = ("secure", "guarantee", "guarantees", "prevents", "sandboxed", "eliminates")

# Every external fact the README asserts has to be traceable to the page it came from.
REQUIRED_CITATIONS = (
    "https://www.mql5.com/en/docs/python_metatrader5/mt5symbolsget_py",
    "https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py",
    "https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfo_py",
    "https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesrange_py",
    "https://modelcontextprotocol.io/specification/2026-07-28/server/tools",
    "https://py.sdk.modelcontextprotocol.io/migration/",
    "https://docs.astral.sh/uv/concepts/projects/dependencies/",
    "https://legal.yahoo.com/us/en/yahoo/terms/product-atos/apiforydn/index.html",
    "https://help.yahoo.com/kb/SLN2310.html",
    "https://www.histdata.com/f-a-q/",
)


def prose(text: str) -> str:
    """`text` with fenced blocks removed, so a transcript cannot satisfy a prose assertion."""
    return re.sub(r"^```.*?^```", "", text, flags=re.MULTILINE | re.DOTALL)


def section(heading: str) -> str:
    body = README.split(f"\n## {heading}\n", 1)[1]
    return body.split("\n## ", 1)[0]


# --------------------------------------------------------------------------------------
# The tool reference table


def test_the_readme_documents_every_tool_in_registration_order() -> None:
    documented = re.findall(r"^\| `(\w+)` \|", section("Tools"), flags=re.MULTILINE)
    assert documented == list(TOOL_NAMES)


def test_the_tool_table_marks_every_tool_read_and_names_both_sources() -> None:
    rows = [row for row in section("Tools").splitlines() if row.startswith("| `")]
    assert len(rows) == len(TOOL_NAMES)
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        # name, arguments, returns, access, replay support, MT5 support
        assert len(cells) == 6, row
        assert cells[3] == "read", row
        assert cells[4] and cells[5], row


# --------------------------------------------------------------------------------------
# The configuration surface


def test_the_readme_states_the_bar_cap_the_code_enforces() -> None:
    caps = section("Quickstart")
    assert f"`{MAX_BARS_DEFAULT}`" in caps
    assert str(MAX_BARS_CEILING) in caps
    assert f"le={MAX_BARS_CEILING}" in section("Safety design")


@pytest.mark.parametrize("variable", [ENV_SOURCE, ENV_SYMBOLS, ENV_MAX_BARS, ENV_LOG_LEVEL])
def test_every_environment_variable_is_documented(variable: str) -> None:
    assert f"`{variable}`" in section("Quickstart")


def test_every_command_line_flag_is_documented() -> None:
    flags = {
        option
        for action in build_parser()._actions
        for option in action.option_strings
        if option.startswith("--") and option not in {"--help", "--version"}
    }
    assert flags
    missing = [flag for flag in flags if f"`{flag}`" not in section("Quickstart")]
    assert missing == []


def test_every_supported_timeframe_is_documented() -> None:
    documented = section("Timeframe aggregation")
    missing = [name for name in TIMEFRAME_SECONDS if name not in documented]
    assert missing == []


def test_the_quickstart_runs_the_console_script_this_package_declares() -> None:
    manifest = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    (script,) = manifest["project"]["scripts"]
    homepage = manifest["project"]["urls"]["Homepage"]
    assert f"uvx --from git+{homepage} {script} --source replay" in README
    # The host snippet has to invoke the same entry point, or one of the two is wrong.
    assert f'"{script}", "--source", "replay"' in section("Connect it to a host")


# --------------------------------------------------------------------------------------
# What the prose is allowed to claim


@pytest.mark.parametrize("word", FORBIDDEN_CLAIMS)
def test_the_readme_makes_no_claim_the_code_cannot_support(word: str) -> None:
    found = re.findall(rf"\b{word}\b", prose(README), flags=re.IGNORECASE)
    assert found == []


def test_the_readme_states_the_read_only_claim_before_anything_else() -> None:
    # In a trading adjacent tool "can this lose someone money" is the first question, so it
    # is answered in the tagline rather than five sections down.
    tagline = README.splitlines()[2]
    assert tagline == "**Market data for agents. Read only by construction, not by configuration.**"


def test_the_readme_describes_both_halves_of_the_read_only_check() -> None:
    # The grep alone was the claim for a while, and a grep is defeated by a computed name.
    # If the AST walk is ever dropped, the prose has to stop advertising it.
    safety = " ".join(section("Safety design").split())
    assert "greps the package" in safety
    assert "walks the AST" in safety
    assert "getattr" in safety


def test_the_readme_admits_the_requirement_that_is_not_implemented() -> None:
    safety = section("Safety design")
    rate_limit = next(row for row in safety.splitlines() if "Rate limit" in row)
    assert "| No |" in rate_limit
    assert "No rate limiting" in section("Limitations")


def test_the_dependency_paragraph_names_every_direct_dependency_of_the_sdk() -> None:
    """The paragraph exists to name the tree, so the tree is where it gets checked.

    It shipped naming ten of mcp 2.0.0's fourteen direct dependencies, with pyjwt and its
    crypto stack and python-multipart among the four it left out. A list that is mostly
    right is worse than no list, because it reads as an audit.
    """
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    sdk = next(package for package in lock["package"] if package["name"] == "mcp")
    named = section("Design decisions")
    missing = [
        dependency["name"] for dependency in sdk["dependencies"] if dependency["name"] not in named
    ]
    assert missing == []


# --------------------------------------------------------------------------------------
# Citations and links


@pytest.mark.parametrize("url", REQUIRED_CITATIONS)
def test_every_required_citation_is_present(url: str) -> None:
    assert url in README


def test_every_link_is_https_or_a_file_that_exists() -> None:
    targets = re.findall(r"\]\(([^)\s]+)\)", prose(README))
    assert len(targets) > 10
    broken = [
        target
        for target in targets
        if not target.startswith("https://") and not (REPO / target).exists()
    ]
    assert broken == []


# --------------------------------------------------------------------------------------
# The changelog


def test_the_changelog_documents_the_version_the_package_reports() -> None:
    assert f"## [{__version__}] - " in CHANGELOG
    assert re.search(rf"## \[{re.escape(__version__)}\] - \d{{4}}-\d{{2}}-\d{{2}}\n", CHANGELOG)
    assert f"[{__version__}]: https://" in CHANGELOG
    assert "https://keepachangelog.com/" in CHANGELOG


def test_the_changelog_release_link_points_at_this_repository_and_this_version() -> None:
    # A hand written URL in a footer is where a repository rename or a version bump goes
    # unnoticed. The tag it names has to be pushed for the link to resolve, which no offline
    # test can check, so this checks the half that can be checked.
    manifest = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    homepage = manifest["project"]["urls"]["Homepage"]
    assert f"[{__version__}]: {homepage}/releases/tag/v{__version__}" in CHANGELOG


def test_the_changelog_names_every_tool_the_server_registers() -> None:
    missing = [name for name in TOOL_NAMES if f"`{name}`" not in CHANGELOG]
    assert missing == []


# --------------------------------------------------------------------------------------
# The workflow behind the badge


def test_the_readme_badge_points_at_a_workflow_that_exists() -> None:
    assert WORKFLOW.is_file()
    assert "actions/workflows/ci.yml/badge.svg" in README


def test_every_action_is_pinned_to_a_released_tag() -> None:
    uses = re.findall(r"^\s*- uses: (\S+)$", WORKFLOW.read_text(encoding="utf-8"), re.MULTILINE)
    assert uses
    # A floating ref such as @main would make a green badge unreproducible. An exact
    # version is accepted as well as a major tag: setup-uv publishes no moving major
    # tag, so @v10 resolves to nothing and the job fails before it runs a step.
    unpinned = [
        entry for entry in uses if not re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+(\.\d+){0,2}", entry)
    ]
    assert unpinned == []
    assert set(uses) == {"actions/checkout@v7", "astral-sh/setup-uv@v10.0.1"}


def test_the_readme_states_the_number_of_tests_this_suite_actually_has() -> None:
    """Collected in a subprocess, because the count has to come from pytest, not from a guess.

    `--collect-only` runs nothing, so this does not recurse; it costs about a second and it
    is the difference between a number that stays true and a number that was true once.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
        cwd=REPO,
    )
    collected = sum(
        int(line.rsplit(": ", 1)[1])
        for line in result.stdout.splitlines()
        if re.fullmatch(r"tests/\S+\.py: \d+", line)
    )
    assert collected > 0
    assert f"{collected} tests" in section("Development")


def test_every_command_the_readme_tells_a_reader_to_run_is_a_command_ci_runs() -> None:
    # Otherwise the Development block drifts into a list of things that used to be checked,
    # and a contributor who runs all of it can still be surprised by a red badge.
    workflow = WORKFLOW.read_text(encoding="utf-8")
    commands = [
        line.strip() for line in section("Development").splitlines() if line.startswith("uv")
    ]
    assert len(commands) >= 4
    missing = [command for command in commands if command not in workflow]
    assert missing == []


def test_the_workflow_covers_every_platform_and_version_the_readme_claims() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    badge = next(line for line in README.splitlines() if "img.shields.io/badge/python" in line)
    for version in re.findall(r"3\.\d+", badge):
        assert f'"{version}"' in workflow, version
    for runner in ("ubuntu-latest", "macos-latest", "windows-latest"):
        assert runner in workflow, runner
    assert "enable-cache: true" in workflow
    # The macOS job is what backs the README's claim that this runs with no MetaTrader
    # install anywhere, so it is asserted rather than left to the matrix.
    assert "uv pip install '.[mt5]'" in workflow


def _escaped(text: str) -> str:
    """The card is HTML, so the captured output appears in it escaped, not raw."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def test_the_committed_demo_output_still_matches_a_live_run() -> None:
    """The Pages card publishes this output, so a stale copy is a lie on a public page.

    The card is generated outside this repository and committed, because the generator is
    deliberately not a repository and no CI job here could check it out. That puts the
    freshness burden here instead, which is the right place: this is the only test suite that
    can run the demo and compare.
    """
    committed = (REPO / "docs" / "evidence" / "demo.txt").read_text(encoding="utf-8")
    live = subprocess.run(
        [sys.executable, "examples/agent_session.py"],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
        cwd=REPO,
    ).stdout
    assert committed == live, (
        "docs/evidence/demo.txt no longer matches a live run. "
        "Run: uv run python scripts/capture_evidence.py, then regenerate the card."
    )


def test_the_published_card_carries_the_output_it_claims_to() -> None:
    card = (REPO / "site" / "index.html").read_text(encoding="utf-8")
    demo = (REPO / "docs" / "evidence" / "demo.txt").read_text(encoding="utf-8")
    assert _escaped(demo.rstrip()) in card, "the card's terminal block is not the captured output"
    # The claim the note on the card makes about itself has to be true, and this is the test
    # it points at. If this assertion is ever deleted, that sentence becomes false.
    assert "a test fails when it" in card


def test_the_card_states_numbers_that_are_true_today() -> None:
    facts = json.loads((REPO / "docs" / "evidence" / "facts.json").read_text(encoding="utf-8"))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "--collect-only", "-q"],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
        cwd=REPO,
    )
    match = re.search(r"^(\d+) tests? collected", result.stdout, re.MULTILINE)
    assert match is not None
    assert facts["tests"] == int(match.group(1)), "facts.json's test total is stale"
    assert facts["release"] == subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"],
        capture_output=True, text=True, check=True, cwd=REPO,
    ).stdout.strip()
    card = (REPO / "site" / "index.html").read_text(encoding="utf-8")
    assert f"<dd>{facts['tests']}</dd>" in card
    assert f"<dd>{facts['release']}</dd>" in card
