"""What this repository's continuous integration actually runs, wherever it is written.

WHY THIS MODULE EXISTS. Two tests here assert that a command the README tells a reader to run is
a command CI runs, by looking for the literal string in `.github/workflows/ci.yml`. That was
sound while every command was written out in that file. It stopped being sound the moment the
common legs moved into the shared workflow in `PNX89/.github`, because the commands are still run
and are no longer in this file.

The naive fixes are both wrong. Deleting the assertions removes the only thing keeping the
Development block honest. Weakening them to "the workflow mentions ruff somewhere" asserts
nothing at all.

WHY THIS IS SOUND RATHER THAN A CONVENIENT LIST. The shared workflow is called at an immutable
tag, and `test_the_shared_workflow_is_pinned_to_a_tag_too` fails if that ever becomes a branch.
A tag cannot change what it runs. So the command set below is a fact about `@v1` rather than a
hopeful description of whatever is on someone else's default branch today, and moving to `@v2`
means updating this list in the same commit as the pin.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO / ".github" / "workflows" / "ci.yml"

# The steps PNX89/.github/.github/workflows/checks.yml@v1 runs, in order. `uv run mypy` is
# conditional on the run-mypy input and is handled below rather than assumed.
SHARED_ALWAYS = (
    "uv sync",
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run pytest",
)
SHARED_IF_MYPY = "uv run mypy"

_CALL = re.compile(r"^\s{4}uses: (\S+/\.github/workflows/\S+)$", re.MULTILINE)
_MYPY_ON = re.compile(r"^\s+run-mypy:\s*true\s*$", re.MULTILINE)


def calls_shared_workflow(text: str | None = None) -> bool:
    return bool(_CALL.search(text if text is not None else WORKFLOW_PATH.read_text("utf-8")))


def runs(command: str, text: str | None = None) -> bool:
    """True when this repository's CI runs `command`, in its own file or in the shared one."""
    workflow = text if text is not None else WORKFLOW_PATH.read_text("utf-8")
    if command in workflow:
        return True
    if not calls_shared_workflow(workflow):
        return False
    if command.startswith(SHARED_IF_MYPY):
        return bool(_MYPY_ON.search(workflow))
    return any(command.startswith(shared) for shared in SHARED_ALWAYS)
