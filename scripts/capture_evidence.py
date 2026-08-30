"""Capture the demo's real output and the numbers the Pages card states.

WHY THIS EXISTS. The card at pnx89.github.io/QUOTEZ shows the output of a real run and four
numbers about this repository. Both are committed, which means both can go stale. This writes
them from the source of truth in one step, and `tests/test_docs.py` fails when what is
committed stops matching a live run, so staleness is a red build rather than a quiet lie on a
public page.

WHAT IT WRITES.
    docs/evidence/demo.txt    stdout of the demo command, byte for byte
    docs/evidence/facts.json  test total, supported Python range, release tag, capture date

Every number comes from a command, never from a memory. The test total is collected by pytest
in a subprocess, the Python range is read out of the CI matrix, and the release is read from
git. Run it with:

    uv run python scripts/capture_evidence.py
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
EVIDENCE = REPO / "docs" / "evidence"
DEMO = ["uv", "run", "python", "examples/agent_session.py"]


def run(*args: str, cwd: pathlib.Path = REPO) -> str:
    result = subprocess.run(args, capture_output=True, text=True, cwd=cwd, timeout=600)
    if result.returncode:
        raise SystemExit(f"{args[0]} failed: {result.stderr.strip()[:400]}")
    return result.stdout


def test_total() -> int:
    """Collected, not counted by hand. -o addopts= neutralises this repository's own `-q`,
    which otherwise prints per-file totals instead of the line this parses."""
    out = run(sys.executable, "-m", "pytest", "-o", "addopts=", "--collect-only", "-q")
    match = re.search(r"^(\d+) tests? collected", out, re.MULTILINE)
    if not match:
        raise SystemExit(f"could not read a collection total from:\n{out[-400:]}")
    return int(match.group(1))


def python_range() -> str:
    """The versions CI will FAIL the build over, read as structure rather than as text.

    THE DOCSTRING USED TO STATE THE PROPERTY THIS FUNCTION DID NOT HAVE. It said it reads the CI
    matrix so the card cannot claim support CI does not test, and it regexed every quoted `3.x`
    in the whole file. That caught `UV_PYTHON: "3.14"` from a job carrying
    `continue-on-error: true`, so the published card advertised Python 3.14 support while the
    blocking matrix stopped at 3.13 and the README badge said so. The page and the badge
    contradicted each other about the same repository, and the page was the one whose own footer
    promises it cannot drift from the code.

    Two rules now, and the second is the one that matters:

      1. Read the YAML, do not pattern-match the file. This repository already learned that in
         `pagesgen/src/map.ts`: a dependency list has to be read as structure and never as text
         that looks like structure. The lesson was written there and not carried here.
      2. A job that is allowed to fail is not a claim of support. An advisory leg is a useful
         thing to run and a dishonest thing to advertise, so `continue-on-error` jobs are skipped
         and the advisory version is mentioned in the README's own words instead.

    Sorted on a tuple of integers, never on `float`. `float("3.9") > float("3.13")`, so a 3.9 leg
    would have published a range running backwards.
    """
    import yaml

    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    versions: set[str] = set()
    for job in (workflow.get("jobs") or {}).values():
        if job.get("continue-on-error"):
            continue
        declared = (job.get("with") or {}).get("python-versions")
        if declared is None:
            continue
        parsed = json.loads(declared) if isinstance(declared, str) else declared
        versions.update(str(v) for v in parsed)
    if not versions:
        raise SystemExit(
            "no gating job declares python-versions, so this card would state a range nothing "
            "verifies"
        )
    ordered = sorted(versions, key=lambda v: tuple(int(part) for part in v.split(".")))
    return f"{ordered[0]} to {ordered[-1]}"


def release() -> str:
    """From the package version, cross-checked against the tag when one is reachable.

    `git describe` alone would be wrong here: a shallow checkout has no tags, and a version
    bumped without tagging would still report the old tag. The version is the claim; the tag
    is checked against it locally, where tags exist, so a mismatch is caught at capture time
    rather than published.
    """
    from quotez import __version__

    tag = f"v{__version__}"
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    described = result.stdout.strip()
    if result.returncode == 0 and described != tag:
        raise SystemExit(
            f"the newest tag is {described} but the package version is {__version__}. "
            "Tag the release or fix the version before publishing a card that names one."
        )
    return tag


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)

    output = run(*DEMO)
    if not output.strip():
        raise SystemExit("the demo produced no output, refusing to write empty evidence")
    (EVIDENCE / "demo.txt").write_text(output, encoding="utf-8")

    # GITHUB_RUN_ID is set only inside Actions. Locally the card says "captured on <date>"
    # with no link rather than inventing one.
    run_id = os.environ.get("GITHUB_RUN_ID")
    repo_slug = os.environ.get("GITHUB_REPOSITORY", "PNX89/QUOTEZ")
    facts = {
        "tests": test_total(),
        "python": python_range(),
        "release": release(),
        "captured": datetime.date.today().isoformat(),
        "runUrl": f"https://github.com/{repo_slug}/actions/runs/{run_id}" if run_id else None,
    }
    (EVIDENCE / "facts.json").write_text(json.dumps(facts, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {EVIDENCE / 'demo.txt'} ({len(output.splitlines())} lines)")
    print(f"wrote {EVIDENCE / 'facts.json'} {facts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
