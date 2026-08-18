"""The README's agent transcript has to be output, not prose.

A market data server whose demo path is synthetic invites exactly one question: is this
real, or staged. The answer has to be mechanical, so the block between the transcript
markers is asserted byte for byte against what `examples/agent_session.py` prints. Edit the
transcript by hand and this fails; change a price, a field or a tool name and this fails
until the README is regenerated.

The script is run in a subprocess with a cleared `QUOTEZ_*` environment, because the point
is that its output cannot depend on where it ran.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
SCRIPT = REPO / "examples" / "agent_session.py"

START = "<!-- transcript:start -->"
END = "<!-- transcript:end -->"


def embedded_transcript() -> str:
    text = README.read_text(encoding="utf-8")
    body = text.split(START, 1)[1].split(END, 1)[0]
    assert body.startswith("\n```\n")
    assert body.endswith("```\n")
    return body[len("\n```\n") : -len("```\n")]


def generated_transcript() -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("QUOTEZ_")}
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
        cwd=REPO,
        env=environment,
    )
    assert result.stderr == ""
    return result.stdout


def test_the_readme_transcript_is_the_scripts_actual_output() -> None:
    assert embedded_transcript() == generated_transcript()


def test_the_readme_names_the_regeneration_command() -> None:
    text = README.read_text(encoding="utf-8")
    assert "uv run python examples/agent_session.py" in text
    # The reader has to be told the prices are invented at the point they read them, not
    # five sections later.
    prose = text.split(START, 1)[0]
    assert "generated" in prose.rsplit("## ", 1)[-1]


def test_the_transcript_shows_both_a_success_and_a_failure() -> None:
    transcript = embedded_transcript()
    assert '"synthetic": true' in transcript
    assert "is_error: true" in transcript
    assert "NOT_A_SYMBOL" in transcript


def test_running_the_script_twice_gives_the_same_bytes() -> None:
    # It reads no clock and no environment, which is the only reason the assertion above is
    # a test rather than a source of intermittent failures.
    assert generated_transcript() == generated_transcript()
