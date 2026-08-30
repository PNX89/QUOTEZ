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

import html
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
from tests import shared_ci
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


def test_every_action_is_pinned_to_a_commit() -> None:
    """A TAG WAS NOT ENOUGH, AND THIS TEST USED TO SAY IT WAS.

    It accepted `@v7` and asserted the exact set of tags in use, which made a floating `@main`
    impossible and a moving `@v7` invisible. A tag is a pointer its owner can move: the workflow
    that passed yesterday can run different code today with no commit here, and the version a
    reader sees still says what it always said.

    So the rule is a commit, with the version in a trailing comment for a human. That is a
    stricter test than the one it replaces and it is checked the same way: by reading what the
    workflow actually says rather than by trusting that somebody remembered.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    uses = re.findall(r"^\s*- uses: (\S+)", text, re.MULTILINE)
    assert uses
    unpinned = [entry for entry in uses if not re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", entry)]
    assert unpinned == [], f"these actions are pinned to something an owner can move: {unpinned}"

    # And the version has to stay beside it. A forty character hash with no version is a pin
    # nobody can read, and the first person who needs to upgrade it will look it up on the
    # internet rather than in the file.
    for line in text.splitlines():
        if re.match(r"^\s*- uses: [\w.-]+/[\w.-]+@[0-9a-f]{40}", line):
            assert "#" in line, f"pinned with no version named beside it: {line.strip()}"


def test_the_shared_workflow_is_pinned_to_a_tag_too() -> None:
    """A reusable workflow on a moving branch is the same defect as a floating action ref.

    The test above only sees step level `- uses:`. A reusable workflow is called at the JOB
    level, without the dash, so it slipped through entirely: `@main` there would let a commit in
    another repository turn this badge red with nothing changed here, which is precisely what
    pinning exists to prevent.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    called = re.findall(r"^\s{4}uses: (\S+)$", text, re.MULTILINE)
    assert called, "no reusable workflow is called, so this repository restates the shared legs"
    unpinned = [
        entry
        for entry in called
        if not re.fullmatch(r"[\w.-]+/[\w.-]+/\.github/workflows/[\w.-]+@v\d+(\.\d+){0,2}", entry)
    ]
    assert unpinned == [], f"a reusable workflow is not pinned to a released tag: {unpinned}"


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
    commands = [
        line.strip() for line in section("Development").splitlines() if line.startswith("uv")
    ]
    assert len(commands) >= 4
    # Asked of the pipeline rather than of this file. The common legs moved into the shared
    # workflow, so a literal search here would now say the README lists commands CI does not run,
    # which is false. shared_ci.runs answers the real question and explains why it is sound.
    missing = [command for command in commands if not shared_ci.runs(command)]
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
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
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
    # EVERY LINE, IN ORDER, RATHER THAN ONE CONTIGUOUS BLOCK. The card folds output longer than
    # forty lines into a <details>, so the transcript arrives in two <pre> elements and a
    # substring test fails on a page that is completely correct. Folding rather than truncating
    # is the point: every byte of the artefact is still on the page, which is what the note under
    # it claims, and the reader gets the argument on the first screen. Asserted in ORDER, so a
    # card carrying the right lines shuffled would still fail.
    position = 0
    for line in demo.rstrip().split("\n"):
        found = card.find(_escaped(line), position)
        assert found >= 0, (
            f"the card is missing a line of the captured output, or has it out of order: "
            f"{line[:70]!r}"
        )
        position = found
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
    # Against the package version, not against `git describe`. actions/checkout clones without
    # tags, so a git-based assertion passes on a developer machine and fails in CI for a reason
    # that has nothing to do with the thing being tested. This is also the stronger claim: it
    # ties the release the card advertises to the version the wheel would carry.
    assert facts["release"] == f"v{__version__}"
    card = (REPO / "site" / "index.html").read_text(encoding="utf-8")
    # EVERY CELL IN THE STRIP, read back with its label, rather than two of the three checked
    # in by value. The cell nobody asserted was the cell that went wrong: the card advertised a
    # Python version CI is allowed to fail over, while the README badge in the same repository
    # said otherwise. A cell added here later fails this instead of arriving unwatched.
    strip = re.findall(r"<dt>\s*([^<]+?)\s*</dt>\s*<dd>\s*([^<]+?)\s*</dd>", card)
    assert strip == [
        ("Tests", str(facts["tests"])),
        ("Python", facts["python"]),
        ("Release", facts["release"]),
    ], f"the card's fact strip is {strip}, which is not what facts.json says"


def test_the_readme_frame_is_built_from_the_captured_output() -> None:
    """The animated frame in the first screenful has to be the real run, not a picture of one.

    Every text line the SVG draws, minus the prompt line it adds and the truncation note it
    ends with, must be the line the captured output printed in that position. The arithmetic
    comes from the frame's own closing note rather than from the generator, because a test that
    reimplements the thing it checks passes for the wrong reason.

    IT USED TO ASSERT NOTHING AT ALL ON A FRAME THAT DREW NOTHING. The body was collected and
    looped over with no floor under it, so blanking every <text> in the SVG except the prompt
    and the note left a frame carrying no output whatsoever, and the loop ran zero times and
    passed. The README offers this frame in its first screenful as the reason the picture is not
    a picture.
    """
    svg = (REPO / "docs" / "demo.svg").read_text(encoding="utf-8")
    demo = (REPO / "docs" / "evidence" / "demo.txt").read_text(encoding="utf-8")

    drawn = [html.unescape(m) for m in re.findall(r"<text[^>]*>(.*?)</text>", svg, re.DOTALL)]
    assert drawn, "the frame draws no text at all"
    assert drawn[0].startswith("$ "), "the frame does not open on the command it ran"
    # drawn[-2] is the blank line the frame puts between the output and the note, which is why
    # the slice below stops two short. Asserted rather than assumed: it used to be excluded with
    # no reason given, so a frame that started drawing real output there would lose a line from
    # the comparison silently.
    assert drawn[-2] == "", "the frame no longer ends on a spacer, so this slice is wrong"

    body = [line for line in drawn[1:-2] if line.strip()]
    haystack = demo.splitlines()

    # HOW MANY LINES THE FRAME OWES, taken from its own closing note rather than from the
    # generator's arithmetic. Without this the loop below runs zero times on a frame that drew
    # nothing, and blanking every <text> in the SVG but the first and the last passed. That is
    # the picture the README's first screenful offers as proof it is not a picture.
    note = re.fullmatch(r"\.\.\. (\d+) more lines, in full on the card", drawn[-1])
    assert note is not None, f"the frame's closing note has changed shape: {drawn[-1]!r}"
    shown = len(haystack) - int(note.group(1))
    assert shown > 0, "the note claims the frame left out everything it was given"
    printed = [line for line in haystack[:shown] if line.strip()]
    assert len(body) == len(printed), (
        "the frame draws a different number of lines from the one its own note accounts for"
    )

    # PAIRED OFF, line against the line it is supposed to be, rather than each frame line
    # searched for anywhere in the run. Scanning forwards was the weaker half of the same
    # mistake the count above fixes: a long enough document contains almost any prefix
    # somewhere. The count makes the pairing possible and the pairing is what makes it mean
    # something.
    #
    # The frame clips a line that will not fit and ends it in an ellipsis, which is legitimate
    # and has to stay passing, so the rule is: a drawn line is either the printed line, or it is
    # a clipped one that says so. Demanding it back verbatim would fail on a correct frame;
    # accepting any prefix would accept a frame that drew one character of it.
    for drawn_line, printed_line in zip(body, printed, strict=True):
        if drawn_line.endswith("..."):
            assert printed_line.startswith(drawn_line[:-3]), (
                f"the frame clips a line the run never printed: {drawn_line!r}"
            )
        else:
            assert drawn_line == printed_line, (
                f"the frame draws {drawn_line!r} where the run printed {printed_line!r}"
            )

    # ASCII only: test_every_text_file_in_the_repository_is_pure_ascii covers the tree, and this
    # says why it matters here. The frame is generated, so a non ASCII glyph would arrive
    # silently from a code change rather than from anyone typing one.
    assert svg.isascii()
    assert "<script" not in svg, "a README image is served through a proxy that strips script"


def test_the_card_claims_only_the_python_versions_ci_will_fail_over() -> None:
    """The card said 3.11 to 3.14. CI's blocking matrix stops at 3.13.

    THE FUNCTION'S DOCSTRING PROMISED THE PROPERTY IT DID NOT HAVE. `python_range()` said it
    reads the CI matrix "so the card cannot claim support CI does not test", and it regexed every
    quoted `3.x` in the whole workflow file. That swept up `UV_PYTHON: "3.14"` from a job marked
    `continue-on-error: true`.

    An advisory leg is a good thing to run and a dishonest thing to advertise. The published card
    claimed a version the build will not fail over, while the README badge in the same repository
    said 3.11 to 3.13, so the two pages contradicted each other about the same fact and the wrong
    one was the page whose own footer promises it cannot drift from the code.

    NOTHING ASSERTED THIS FIELD ANYWHERE. The card test checked `tests` and `release` into the
    page and skipped `python`. Three-way here: the gating matrix, the facts file, and the badge a
    reader sees first.
    """
    import yaml

    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
    gating: set[str] = set()
    advisory: set[str] = set()
    for job in (workflow.get("jobs") or {}).values():
        declared = (job.get("with") or {}).get("python-versions")
        env_pin = (job.get("env") or {}).get("UV_PYTHON")
        found = set()
        if declared is not None:
            found |= {
                str(v) for v in (json.loads(declared) if isinstance(declared, str) else declared)
            }
        if env_pin:
            found.add(str(env_pin))
        (advisory if job.get("continue-on-error") else gating).update(found)

    assert gating, "no job gates on a Python version, so the card's range verifies nothing"
    order = lambda v: tuple(int(part) for part in v.split("."))  # noqa: E731
    lowest, highest = min(gating, key=order), max(gating, key=order)

    facts = json.loads((REPO / "docs" / "evidence" / "facts.json").read_text("utf-8"))
    assert facts["python"] == f"{lowest} to {highest}", (
        f"the card states Python {facts['python']} and CI gates on {lowest} to {highest}"
    )

    # The badge a reader meets before the card, so the two pages cannot disagree.
    readme = (REPO / "README.md").read_text("utf-8")
    for version in sorted(gating, key=order):
        assert version in readme, f"CI gates on {version} and the README never mentions it"

    # An advisory version must NOT be inside the claimed range, which is the exact defect.
    for version in advisory - gating:
        assert not (order(lowest) <= order(version) <= order(highest)), (
            f"{version} runs only in a job allowed to fail and the card claims it as supported"
        )
