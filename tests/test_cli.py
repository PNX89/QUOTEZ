"""Tests for the command line, and for the hygiene rules that keep stdio usable.

On stdio, stdout is the JSON-RPC transport. A stray `print`, a banner or a log record sent
to the wrong stream corrupts it, and the failure is a protocol parse error somewhere else
entirely. Three of these tests exist only to make that impossible to reintroduce, including
one that runs the real console script end to end and asserts stdout is empty to the byte.

The environment tests run the installed console script in a subprocess rather than calling
`main`. That is not belt and braces: the bug they exist to catch happened at IMPORT, so a
test in this process could never see it, because the import had already succeeded before
the test started.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from quotez import __version__
from quotez.cli import main

REPO = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO / "src" / "quotez"
QUOTEZ = Path(sys.executable).parent / "quotez"

# Em dash and en dash, spelled as escapes so this file cannot fail its own check.
DASHES = ("\u2014", "\u2013")


def source_files() -> list[Path]:
    return sorted(SOURCE_DIR.rglob("*.py"))


def run_console(*args: str, **environment: str) -> subprocess.CompletedProcess[str]:
    """Run the installed console script with a clean `QUOTEZ_*` environment plus `environment`."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("QUOTEZ_")}
    return subprocess.run(
        [str(QUOTEZ), *args],
        input="",
        capture_output=True,
        text=True,
        timeout=60,
        env=env | environment,
    )


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """Undo what `main` does to the root logger, so a captured stream cannot outlive a test."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.level = level


# --------------------------------------------------------------------------------------
# Flags


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
    assert capsys.readouterr().out == f"quotez {__version__}\n"


def test_an_unknown_source_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--source", "binance"])
    assert caught.value.code == 2
    captured = capsys.readouterr()
    # argparse writes usage to stderr, and it happens before serving begins either way.
    assert captured.out == ""
    assert "invalid choice" in captured.err


def test_help_lists_every_documented_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    text = capsys.readouterr().out
    for flag in ("--source", "--symbols", "--max-bars", "--log-level", "--version"):
        assert flag in text


def test_logging_is_pointed_at_stderr_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    # Importing the SDK installs a root handler, and basicConfig is a no-op once one
    # exists. Without force=True this configuration would silently not happen and the
    # destination of every record would be somebody else's default.
    monkeypatch.setattr("quotez.server.MCPServer.run", lambda self, *a, **k: None)
    assert main(["--log-level", "WARNING"]) == 0
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert type(handlers[0]) is logging.StreamHandler
    assert handlers[0].stream is sys.stderr


def test_a_startup_failure_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from quotez.errors import SourceUnavailable

    def refuse(self: object, *_args: object, **_kwargs: object) -> None:
        raise SourceUnavailable("MetaTrader 5 initialize() failed with code -10005.")

    monkeypatch.setattr("quotez.server.MCPServer.run", refuse)
    assert main(["--source", "replay"]) == 1


def test_a_failure_wrapped_in_an_exception_group_still_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # anyio raises exception groups, so a lifespan failure does not arrive bare.
    from quotez.errors import SourceUnavailable

    def refuse(self: object, *_args: object, **_kwargs: object) -> None:
        raise BaseExceptionGroup("lifespan", [SourceUnavailable("terminal not running")])

    monkeypatch.setattr("quotez.server.MCPServer.run", refuse)
    assert main([]) == 1


@pytest.mark.parametrize("grouped", [False, True], ids=["bare", "grouped"])
def test_an_interrupt_is_a_normal_shutdown(monkeypatch: pytest.MonkeyPatch, grouped: bool) -> None:
    def interrupt(self: object, *_args: object, **_kwargs: object) -> None:
        if grouped:
            raise BaseExceptionGroup("serve", [KeyboardInterrupt()])
        raise KeyboardInterrupt

    monkeypatch.setattr("quotez.server.MCPServer.run", interrupt)
    assert main([]) == 0


def test_an_unexpected_failure_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the two known shapes are handled. Anything else keeps its traceback.
    def explode(self: object, *_args: object, **_kwargs: object) -> None:
        raise BaseExceptionGroup("serve", [RuntimeError("something else entirely")])

    monkeypatch.setattr("quotez.server.MCPServer.run", explode)
    with pytest.raises(BaseExceptionGroup):
        main([])


# --------------------------------------------------------------------------------------
# Stdio hygiene


def test_the_package_contains_no_print_call() -> None:
    offenders = [
        path.relative_to(REPO)
        for path in source_files()
        if re.search(r"\bprint\s*\(", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_importing_and_building_the_server_writes_nothing_to_stdout() -> None:
    # A subprocess, because the import has already happened in this one and an import time
    # write is exactly what this is looking for.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from quotez.config import ServerConfig\n"
            "from quotez.server import build_server, mcp\n"
            "build_server(ServerConfig())\n",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert result.stdout == ""


@pytest.mark.skipif(not QUOTEZ.exists(), reason="console script not installed")
def test_the_console_script_serves_and_leaves_stdout_untouched() -> None:
    # The real entry point, with stdin at end of file so the server shuts down at once.
    # Every byte on stdout would be a byte on the JSON-RPC wire.
    result = subprocess.run(
        [str(QUOTEZ), "--source", "replay", "--log-level", "DEBUG"],
        input="",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert "Starting QUOTEZ" in result.stderr


@pytest.mark.skipif(not QUOTEZ.exists(), reason="console script not installed")
def test_the_console_script_reports_its_version_on_stdout() -> None:
    result = subprocess.run(
        [str(QUOTEZ), "--version"], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout == f"quotez {__version__}\n"


# --------------------------------------------------------------------------------------
# The environment, read at run time and not at import time


def test_importing_the_package_reads_no_environment_variable() -> None:
    # The root cause, asserted at its root. `quotez.cli` imports `quotez.server` to reach
    # build_server, so a module level `mcp = build_server(ServerConfig.from_env())` made
    # every malformed QUOTEZ_* variable fatal before main() ran, including for --help.
    env = {key: value for key, value in os.environ.items() if not key.startswith("QUOTEZ_")}
    malformed = {
        "QUOTEZ_MAX_BARS": "abc",
        "QUOTEZ_SOURCE": "binance",
        "QUOTEZ_LOG_LEVEL": "loud",
    }
    result = subprocess.run(
        [sys.executable, "-c", "import quotez.server, quotez.cli\n"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env | malformed,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


@pytest.mark.skipif(not QUOTEZ.exists(), reason="console script not installed")
@pytest.mark.parametrize("flag", ["--version", "--help"])
def test_the_usage_flags_survive_a_malformed_environment(flag: str) -> None:
    result = run_console(flag, QUOTEZ_MAX_BARS="abc", QUOTEZ_SOURCE="binance")
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.skipif(not QUOTEZ.exists(), reason="console script not installed")
@pytest.mark.parametrize(
    ("variable", "value", "flag"),
    [
        ("QUOTEZ_MAX_BARS", "abc", ["--max-bars", "50"]),
        ("QUOTEZ_MAX_BARS", "999999", ["--max-bars", "50"]),
        ("QUOTEZ_SOURCE", "binance", ["--source", "replay"]),
        ("QUOTEZ_LOG_LEVEL", "chatty", ["--log-level", "INFO"]),
    ],
)
def test_a_flag_overrides_a_malformed_environment_default_in_the_real_binary(
    variable: str, value: str, flag: list[str]
) -> None:
    # `build_parser` says defaults are left unvalidated so that a bad variable is not fatal
    # when the caller replaced it. Asserting that in process proves nothing, because the
    # import that used to fail happens before any test runs.
    result = run_console(*flag, **{variable: value})
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "Traceback" not in result.stderr


@pytest.mark.skipif(not QUOTEZ.exists(), reason="console script not installed")
@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("QUOTEZ_MAX_BARS", "abc"),
        ("QUOTEZ_MAX_BARS", "999999"),
        ("QUOTEZ_SOURCE", "binance"),
        ("QUOTEZ_LOG_LEVEL", "chatty"),
    ],
)
def test_an_unoverridden_malformed_variable_exits_two_the_way_an_argument_error_does(
    variable: str, value: str
) -> None:
    # A host with a typo in its config env block should read "quotez: error: ..." and a
    # usage line, not a Python stack trace, and the exit code is the one the module
    # docstring promises for an argument error.
    result = run_console(**{variable: value})
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "usage: quotez" in result.stderr
    assert result.stdout == ""


# --------------------------------------------------------------------------------------
# Prose hygiene


def repository_text_files() -> list[Path]:
    """Every readable text file in the repository, including LICENSE and CI workflows.

    Selected by exclusion rather than by extension, so a file type nobody thought of, such
    as a workflow YAML added later, is covered the day it appears.
    """
    skipped = {".git", ".venv", ".ruff_cache", ".pytest_cache", "__pycache__", "uv.lock"}
    return [
        path for path in sorted(REPO.rglob("*")) if path.is_file() and not skipped & set(path.parts)
    ]


def test_no_file_in_the_repository_uses_an_em_or_en_dash() -> None:
    checked, offenders = 0, []
    for path in repository_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        checked += 1
        offenders += [(str(path.relative_to(REPO)), dash) for dash in DASHES if dash in text]
    assert checked > 20
    assert offenders == []
