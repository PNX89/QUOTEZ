"""The doc-drift contract names a test per kind. This asserts the test still does the work.

`tests/test_doc_contract.py` is generated from the shared manifest and regenerated in place, so
the weaker half of it cannot be strengthened there. This file sits beside it and does that here.

What it strengthens: that file concatenates the suite and looks for the substring "def <name>(",
which is a claim about a NAME and nothing more. Two mutations proved it. Replacing the body of
the transcript comparison, the byte-for-byte README-versus-script check, with one true assertion
left the whole suite green. So did renaming the function outright and leaving its old name
behind in a comment on the line above, because a comment is text like any other.

A name is not an implementation. What follows resolves each named test through the AST of the
file that defines it, so a comment cannot stand in for a function, and then asks whether the
body still computes anything: at least one assertion, and not one made of literals. The OUTPUT
kind gets a second check naming both halves of the comparison it exists to make, since that is
the one whose whole content is that the two sides are produced by different means.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests import test_readme
from tests.test_doc_contract import IMPLEMENTATIONS

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"

# The kinds this file knows how to check, pinned by name and by size. IMPLEMENTATIONS is read
# from the generated file rather than copied, so a kind added to the manifest fails HERE and
# somebody extends the checking, instead of quietly arriving with nothing watching it.
KNOWN_KINDS = frozenset({"NUMBER", "COMMAND", "OUTPUT", "REFERENCE"})


def defined_tests() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every top level test function in the suite, by name, parsed rather than searched for.

    Parsing is the whole point. The generated contract test greps the concatenated source, so a
    name in a comment, in a docstring or in a string literal satisfies it just as well as a
    function does.
    """
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for path in sorted(TESTS.glob("test_*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                found[node.name] = node
    return found


def reaches_for_a_value(test: ast.expr) -> bool:
    """True when an assertion goes and gets something instead of restating a literal.

    `assert True` is the obvious gutting and `assert 1 == 1` is the same move in a shape that a
    check for a bare constant would miss, so the question asked is whether anything in the
    expression is a name, a call, an attribute or a subscript.
    """
    return any(
        isinstance(node, ast.Name | ast.Call | ast.Attribute | ast.Subscript)
        for node in ast.walk(test)
    )


def test_the_contract_kinds_are_the_four_this_file_knows_how_to_check() -> None:
    assert set(IMPLEMENTATIONS) == KNOWN_KINDS, (
        f"the contract manifest now carries {sorted(set(IMPLEMENTATIONS) ^ KNOWN_KINDS)}, which "
        "nothing in this file checks the implementation of"
    )
    assert len(IMPLEMENTATIONS) == 4


@pytest.mark.parametrize("kind", sorted(KNOWN_KINDS))
def test_each_contract_kind_is_a_function_that_still_asserts_something(kind: str) -> None:
    name = IMPLEMENTATIONS[kind]
    function = defined_tests().get(name)
    assert function is not None, (
        f"the {kind} contract names {name}, which is not a test function in this suite. The "
        "generated contract check is satisfied by that name appearing in a comment."
    )
    assertions = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    assert assertions, f"{name} implements the {kind} contract and asserts nothing at all"
    assert any(reaches_for_a_value(node.test) for node in assertions), (
        f"every assertion in {name} is made of literals, so the {kind} contract is checked by a "
        "function that reads nothing"
    )


def test_the_output_contract_still_compares_the_readme_against_a_real_run() -> None:
    """The OUTPUT kind's entire content is that the two sides come from different places.

    One is the block committed inside the README's transcript markers. The other is the stdout
    of a subprocess that runs the example script with a cleared environment. Gutting the body to
    a single true assertion left the generated contract check green, because it was looking at
    the function's name.
    """
    name = IMPLEMENTATIONS["OUTPUT"]
    function = defined_tests().get(name)
    assert function is not None, f"the OUTPUT contract names {name} and no such function exists"
    called = {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"embedded_transcript", "generated_transcript"} <= called, (
        f"{name} no longer calls both halves of the comparison: {called}"
    )
    # Both have to be real callables and not merely names in a call expression, since the AST
    # above would be just as happy with two functions that no longer exist.
    assert callable(test_readme.embedded_transcript)
    assert callable(test_readme.generated_transcript)
