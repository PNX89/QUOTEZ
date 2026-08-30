"""Tests for the MCP surface itself.

These go through the SDK's in-process client, so they assert what a host would actually
receive: the tool list and its order, the annotations, the generated schemas, the
structured content, and the resource. Calling the handlers directly would skip all of it.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Container
from pathlib import Path

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.types import Tool

from quotez import __version__
from quotez.config import ServerConfig
from quotez.models import Quote, SymbolSpec
from quotez.mt5source import Mt5Source
from quotez.replay import ReplaySource
from quotez.server import build_server, make_source
from tests.conftest import SYMBOL, TOOL_NAMES, connected

REPO = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO / "src" / "quotez"
MT5_SOURCE = (SOURCE_DIR / "mt5source.py").read_text(encoding="utf-8")
README = (REPO / "README.md").read_text(encoding="utf-8")

# The three MetaTrader calls that would make the read-only claim a matter of discipline
# instead of a property of the code. Tied to the README's Scope and limits bullet below,
# because both halves of the check key off this tuple and nothing held it to anything:
# shrinking it to two names, or emptying it, left the whole suite green.
WRITE_CALLS = ("order_send", "order_check", "symbol_select")

# Every terminal call this package is allowed to make, kept HERE rather than derived from the
# module being audited. `documented_read_calls()` reads the same list out of `mt5source.py`'s
# own docstring and the two are asserted against each other, so widening what the package
# touches takes an edit to two files in two roles. When the docstring WAS the specification, a
# call and its own permission slip travelled in one commit: adding `market_book_add`, which
# subscribes the terminal to a Depth of Market feed and holds that subscription until it is
# released, is exactly the side effect `symbol_select` is excluded to avoid, and it passed.
PERMITTED_READ_CALLS = frozenset(
    {
        "symbols_get",
        "symbol_info",
        "symbol_info_tick",
        "account_info",
        "positions_get",
        "orders_get",
        "copy_rates_from_pos",
        "copy_rates_range",
        "initialize",
        "shutdown",
        "last_error",
    }
)

# Names that hold the MetaTrader5 module, and the two helpers that hand it back.
TERMINAL_NAMES = frozenset({"MetaTrader5", "mt5"})
TERMINAL_FACTORIES = frozenset({"_mt5", "_terminal"})

# Builtins and helpers that read an attribute off an object under a name computed at run time.
# `attrgetter` belongs here even though it never receives the module itself: it is built first
# and applied to the module afterwards, so the module is the argument of the call it returns.
DYNAMIC_READERS = frozenset({"getattr", "setattr", "delattr", "vars", "dir", "attrgetter"})

TIMEFRAME_CONSTANTS = frozenset(
    f"TIMEFRAME_{period}" for period in ("M1", "M5", "M15", "M30", "H1", "H4", "D1")
)


def is_terminal(node: ast.expr, holders: Container[str] = TERMINAL_NAMES) -> bool:
    """True when `node` evaluates to the MetaTrader5 module.

    `holders` is every expression text currently known to hold the module, which grows as
    bindings are followed. Matching on the unparsed text rather than on an identifier is what
    lets a module parked somewhere other than a bare name be recognised on the way out again.
    """
    if isinstance(node, ast.NamedExpr):
        return is_terminal(node.value, holders)
    if isinstance(node, ast.Call):
        function = node.func
        if isinstance(function, ast.Name):
            return function.id in TERMINAL_FACTORIES
        if isinstance(function, ast.Attribute):
            return function.attr in TERMINAL_FACTORIES
        return False
    return ast.unparse(node) in holders


def _bound_names(target: ast.expr) -> list[str]:
    """The expression texts `target` binds. A tuple target binds each of its elements."""
    if isinstance(target, ast.Tuple | ast.List):
        return [name for element in target.elts for name in _bound_names(element)]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    if isinstance(target, ast.Name | ast.Attribute):
        return [ast.unparse(target)]
    return []


def _binding_pairs(tree: ast.AST) -> list[tuple[ast.expr, ast.expr]]:
    """Every (target, value) pair in `tree`, across all the ways a name gets bound.

    A plain `terminal = mt5` used to be the only form the walk could see. An annotated
    assignment, a walrus, a tuple unpack, a `for` target and a `with ... as` bind just as
    effectively and every one of them went straight past it.
    """
    pairs: list[tuple[ast.expr, ast.expr]] = []

    def bind(target: ast.expr, value: ast.expr | None) -> None:
        if value is None:
            return
        if isinstance(target, ast.Tuple | ast.List) and isinstance(value, ast.Tuple | ast.List):
            # `terminal, other = mt5, None` binds element by element, so pairing the whole
            # target with the whole value would lose which name ended up with the module.
            for element, element_value in zip(target.elts, value.elts, strict=False):
                bind(element, element_value)
            return
        pairs.append((target, value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, node.value)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            bind(node.target, node.value)
        elif isinstance(node, ast.For | ast.AsyncFor):
            bind(node.target, node.iter)
            if isinstance(node.iter, ast.Tuple | ast.List):
                # Iterating a literal binds the ELEMENTS, one per pass, not the sequence.
                for element in node.iter.elts:
                    bind(node.target, element)
        elif isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                if item.optional_vars is not None:
                    bind(item.optional_vars, item.context_expr)
    return pairs


def _dynamic_reader(function: ast.expr) -> str | None:
    """The by-name attribute reader `function` resolves to, if it is one.

    Recursive through a call because `attrgetter("symbol_" + "select")(mt5)` builds the reader
    first and applies it to the module second, so the module is the OUTER call's argument.
    """
    if isinstance(function, ast.Name) and function.id in DYNAMIC_READERS:
        return function.id
    if isinstance(function, ast.Attribute) and function.attr in DYNAMIC_READERS:
        # `builtins.getattr` and `operator.attrgetter` reach the same readers through a dot.
        return function.attr
    if isinstance(function, ast.Call):
        return _dynamic_reader(function.func)
    return None


def terminal_holders(tree: ast.AST) -> tuple[set[str], list[str]]:
    """Every expression holding the terminal module in `tree`, and the aliases among them.

    Grown to a fixed point rather than in a single pass: an alias can be bound from another
    alias, and a binding can be written below the use that reads it.
    """
    pairs = _binding_pairs(tree)
    holders = set(TERMINAL_NAMES)
    aliases: list[str] = []
    growing = True
    while growing:
        growing = False
        for target, value in pairs:
            if not is_terminal(value, holders):
                continue
            for name in _bound_names(target):
                if name in TERMINAL_NAMES:
                    # `mt5 = self._terminal()` is the module arriving under its own name. That
                    # is the idiom every method here uses and it is not a second variable.
                    continue
                if name not in holders:
                    holders.add(name)
                    growing = True
                if name not in aliases:
                    aliases.append(name)
    return holders, aliases


def terminal_access(source: str) -> tuple[set[str], list[str], list[str]]:
    """What `source` touches on the terminal module: attributes, dynamic reads, aliases.

    A grep for "order_send" is satisfied by `getattr(mt5, "order_" + "send")`, which is why
    the read-only claim needs a walk rather than a search. The attribute set is only worth
    anything alongside the other two returns: a name computed at run time never appears in
    the set, and neither does one reached through a second variable holding the same module.

    The module is followed by DATAFLOW rather than by a fixed pair of names. Recognising only
    `MetaTrader5`, `mt5` and a plain assignment left six other ways of binding or reaching it
    unwatched, and any one of them combined with a computed attribute name defeated both halves
    of the claim at once: the grep never sees the literal string and the walk never sees the
    module.
    """
    tree = ast.parse(source)
    holders, aliases = terminal_holders(tree)
    attributes: set[str] = set()
    dynamic: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and is_terminal(node.value, holders):
            attributes.add(node.attr)
        # Every argument, not only the first: attrgetter hands the module to the call it
        # returns rather than taking it itself.
        elif (
            isinstance(node, ast.Call)
            and _dynamic_reader(node.func) is not None
            and any(is_terminal(argument, holders) for argument in node.args)
        ):
            dynamic.append(ast.unparse(node))
    return attributes, dynamic, aliases


def documented_read_calls() -> frozenset[str]:
    """The calls `mt5source.py`'s own docstring claims are the only ones it makes."""
    docstring = ast.get_docstring(ast.parse(MT5_SOURCE)) or ""
    listed = docstring.split("Only read calls are used:", 1)[1].split(".", 1)[0]
    return frozenset(name.strip() for name in listed.replace("\n", " ").split(","))


async def tool_map(client: Client) -> dict[str, Tool]:
    return {tool.name: tool for tool in (await client.list_tools()).tools}


# --------------------------------------------------------------------------------------
# The tool list


@pytest.mark.anyio
async def test_tools_list_contains_exactly_the_eight_expected_names(client: Client) -> None:
    listed = [tool.name for tool in (await client.list_tools()).tools]
    assert listed == list(TOOL_NAMES)


@pytest.mark.anyio
async def test_tool_order_is_the_registration_order_across_builds() -> None:
    # tools/list order is what clients cache, and a stable order improves prompt cache hit
    # rates. Building the list from a set or a dict comprehension would pass once and drift.
    async with connected() as first, connected(ServerConfig(max_bars=42)) as second:
        assert [tool.name for tool in (await first.list_tools()).tools] == list(TOOL_NAMES)
        assert [tool.name for tool in (await second.list_tools()).tools] == list(TOOL_NAMES)


@pytest.mark.anyio
async def test_every_tool_is_declared_read_only_and_closed_world(client: Client) -> None:
    for tool in (await client.list_tools()).tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.open_world_hint is False, tool.name
        # destructive_hint and idempotent_hint are defined by the spec only for tools that
        # are not read only, so leaving them unset is correct rather than an omission.
        assert tool.annotations.destructive_hint is None, tool.name
        assert tool.annotations.idempotent_hint is None, tool.name


@pytest.mark.anyio
async def test_every_tool_has_a_title_a_description_and_an_output_schema(
    client: Client,
) -> None:
    for tool in (await client.list_tools()).tools:
        assert tool.title, tool.name
        # The docstring IS the tool description the model reads.
        assert tool.description and len(tool.description) > 60, tool.name
        assert tool.output_schema, tool.name
        assert tool.output_schema.get("type") == "object", tool.name


@pytest.mark.anyio
async def test_every_tool_reads_and_none_offers_to_write(client: Client) -> None:
    for tool in (await client.list_tools()).tools:
        # The whole surface is getters and listers. A mutating tool could not be named
        # inside this scheme without the name itself giving it away.
        assert tool.name.startswith(("get_", "list_")) or tool.name == "symbol_info"
        assert tool.title is not None and tool.title.split()[0] in {"Get", "List"}
        # The write calls are not offered, not described and not hinted at.
        for call in WRITE_CALLS:
            assert call not in f"{tool.name} {tool.title} {tool.description}"


def test_the_source_tree_contains_no_write_path() -> None:
    # The read-only claim is structural, not a setting: these calls are absent from the
    # package, so no configuration can turn them on. Cheap, whole tree, and defeated by a
    # computed name, which is what the AST walk below exists for.
    sources = sorted(SOURCE_DIR.rglob("*.py"))
    assert sources
    for call in WRITE_CALLS:
        offenders = [
            path.name
            for path in sources
            if f"{call}(" in path.read_text(encoding="utf-8")
            or f".{call}" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], (call, offenders)


def test_the_terminal_is_touched_only_through_the_calls_the_module_documents() -> None:
    """The load bearing version of the read-only claim, and the reason it is not a grep.

    `_mt5()` hands back the whole MetaTrader5 module, so absence of three call names says
    nothing about the other several hundred attributes on it. This asserts the positive
    property instead: the set of attributes this package reads off that module is exactly
    the eleven read calls this file permits, plus the seven timeframe constants, and the
    module is never reached by a computed name or bound to a second variable.

    Against the list in this file, never against the audited module's own docstring. The
    docstring is the declaration a reader meets and it is asserted against this list by
    `test_the_read_allowlist_and_the_module_docstring_say_the_same_thing`; deriving the
    allowlist from it instead let a new terminal call carry its own permission.
    """
    attributes, dynamic, aliases = terminal_access(MT5_SOURCE)
    assert dynamic == []
    assert aliases == []
    assert attributes - TIMEFRAME_CONSTANTS == PERMITTED_READ_CALLS
    assert attributes >= TIMEFRAME_CONSTANTS
    assert not attributes & set(WRITE_CALLS)


def test_the_read_allowlist_and_the_module_docstring_say_the_same_thing() -> None:
    """An allowlist that lives inside the file it audits is not an allowlist.

    It used to be derived from `mt5source.py`'s own "Only read calls are used:" list, so a new
    terminal call and the permission for it landed in a single edit to a single file. That is
    not theoretical: `market_book_add` subscribes the terminal to a Depth of Market feed and
    holds the subscription open, which is the very side effect `symbol_select` is excluded to
    avoid, and adding it in both places at once passed the whole suite.

    The docstring is still the declaration a reader meets. This asserts it against a list kept
    in the test file, so the two now have to be changed in two roles rather than in one place.
    """
    assert documented_read_calls() == PERMITTED_READ_CALLS
    # Pinned by size as well as by contents, because a list that quietly loses an entry reads
    # exactly like one that did not. Both docstrings above say eleven.
    assert len(PERMITTED_READ_CALLS) == 11


def test_the_write_call_denylist_is_the_one_the_readme_publishes() -> None:
    """WRITE_CALLS drives the grep and the walk, and nothing held it to anything.

    Cutting it to two names left the suite green while both checks quietly stopped looking for
    the third. Emptying it entirely left the suite green too. The README's Scope and limits
    bullet names these three calls to every reader of this repository, so that sentence is the
    specification and this is the tie to it.
    """
    bullet = next(line for line in README.splitlines() if line.startswith("- Read only: no "))
    assert re.findall(r"`(\w+)`", bullet) == list(WRITE_CALLS)


# Every route to the terminal that has to keep failing this check, with the channel it has to
# fail on. The first five are the original set. The last six all went straight past the walk,
# and any of them paired with a computed attribute name beat the grep in the same stroke, which
# is how a live MarketWatch mutation sat in the package with the suite reporting green.
BROKEN_SNIPPETS: tuple[tuple[str, str, str], ...] = (
    (
        "direct",
        "def f(mt5):\n    return mt5.symbol_select('X', True)\n",
        "attributes",
    ),
    (
        "computed-name",
        "def f(mt5):\n    return getattr(mt5, 'symbol_' + 'select')('X')\n",
        "dynamic",
    ),
    (
        "alias",
        "def f(mt5):\n    terminal = mt5\n    return terminal.symbol_select('X')\n",
        "aliases",
    ),
    (
        "factory",
        "def f():\n    return _mt5().symbol_select('X')\n",
        "attributes",
    ),
    (
        "method-factory",
        "class S:\n    def f(self):\n        return self._terminal().symbol_select('X')\n",
        "attributes",
    ),
    (
        "annotated-alias",
        "def f(mt5):\n    terminal: object = mt5\n    return terminal.symbol_select('X')\n",
        "aliases",
    ),
    (
        "walrus-alias",
        "def f(mt5):\n    return (terminal := mt5).symbol_select('X')\n",
        "aliases",
    ),
    (
        "tuple-alias",
        "def f(mt5):\n    terminal, _ = mt5, None\n    return terminal.symbol_select('X')\n",
        "aliases",
    ),
    (
        "for-target-alias",
        "def f(mt5):\n    for terminal in (mt5,):\n        return terminal.symbol_select('X')\n",
        "aliases",
    ),
    (
        "attrgetter",
        "import operator\n\n"
        "def f(mt5):\n"
        "    return operator.attrgetter('symbol_' + 'select')(mt5)\n",
        "dynamic",
    ),
    (
        "dotted-getattr",
        "import builtins\n\ndef f(mt5):\n    return builtins.getattr(mt5, 'symbol_' + 'select')\n",
        "dynamic",
    ),
)


def test_every_route_to_the_terminal_is_still_in_the_broken_snippet_set() -> None:
    # Pinned by name and by size. Reading the cases straight off the parametrisation would let
    # a deleted entry leave the suite covering one route fewer and still green, which reads
    # exactly like a pass.
    assert [name for name, _snippet, _channel in BROKEN_SNIPPETS] == [
        "direct",
        "computed-name",
        "alias",
        "factory",
        "method-factory",
        "annotated-alias",
        "walrus-alias",
        "tuple-alias",
        "for-target-alias",
        "attrgetter",
        "dotted-getattr",
    ]
    assert len(BROKEN_SNIPPETS) == 11


@pytest.mark.parametrize(
    ("snippet", "caught_by"),
    [(snippet, channel) for _name, snippet, channel in BROKEN_SNIPPETS],
    ids=[name for name, _snippet, _channel in BROKEN_SNIPPETS],
)
def test_the_boundary_check_catches_the_write_a_grep_would_miss(
    snippet: str, caught_by: str
) -> None:
    # A checker nobody has seen fail is a checker nobody knows works. The computed-name case
    # is the one that matters: the string "symbol_select" never appears in that source.
    attributes, dynamic, aliases = terminal_access(snippet)
    found = {"attributes": attributes, "dynamic": dynamic, "aliases": aliases}[caught_by]
    assert found, snippet
    if caught_by == "attributes":
        assert attributes - TIMEFRAME_CONSTANTS != PERMITTED_READ_CALLS


# --------------------------------------------------------------------------------------
# Server metadata


@pytest.mark.anyio
async def test_instructions_name_the_bar_cap_and_the_read_only_scope() -> None:
    async with connected(ServerConfig(max_bars=321)) as client:
        instructions = client.instructions
    assert instructions
    assert "321" in instructions
    assert "No order placement" in instructions
    assert "list_symbols first" in instructions
    assert "UTC" in instructions


@pytest.mark.anyio
async def test_the_server_identifies_itself_with_the_package_version(client: Client) -> None:
    info = client.server_info
    assert info is not None
    assert info.name == "QUOTEZ"
    assert info.title == "QUOTEZ market data"
    assert info.version == __version__


def test_the_module_global_is_built_lazily_and_only_once() -> None:
    # `mcp run src/quotez/server.py` resolves the server by probing the module for the names
    # mcp, server and app, so the global has to answer hasattr and getattr. It is built on
    # first access rather than at import, which is what keeps a malformed QUOTEZ_* variable
    # from killing the console script before argparse can override it.
    import quotez.server as module

    assert module._default_server is None or isinstance(module._default_server, MCPServer)
    assert hasattr(module, "mcp")
    first = module.mcp
    assert isinstance(first, MCPServer)
    assert module.mcp is first
    with pytest.raises(AttributeError, match="no attribute 'app'"):
        module.app  # noqa: B018


def test_building_an_mt5_server_touches_no_terminal() -> None:
    # Construction is pure: the connection belongs to the lifespan, because
    # mt5.initialize() launches the terminal and is bounded by a timeout defaulting to
    # 60000 milliseconds. Building the mt5 server on a machine with no wheel available has
    # to work, or `quotez --source mt5 --help` would fail on macOS.
    build_server(ServerConfig(source="mt5"))
    assert "MetaTrader5" not in sys.modules


def test_the_source_flag_picks_the_source_it_names() -> None:
    """`make_source` is the single dispatch between a live terminal and the bundled files.

    Nothing asserted what it returns. The test above is satisfied by an mt5 branch that has
    stopped existing, since a replay source imports no terminal either, and every other test
    involving mt5 either builds `Mt5Source` directly or replaces this function outright. Making
    the mt5 branch return `ReplaySource()` left the whole suite green, so `--source mt5` could
    have served generated prices to somebody who asked for their broker's.
    """
    assert isinstance(make_source(ServerConfig(source="mt5")), Mt5Source)
    assert isinstance(make_source(ServerConfig()), ReplaySource)
    # The stamp that reaches the model, not only the class, because `synthetic: true` on every
    # payload is what tells it these prices are invented.
    assert (make_source(ServerConfig(source="mt5")).name, make_source(ServerConfig()).name) == (
        "mt5",
        "replay",
    )


def _flattened(error: BaseException) -> list[BaseException]:
    """`error`, and everything nested inside it when it is an exception group."""
    if isinstance(error, BaseExceptionGroup):
        return [inner for child in error.exceptions for inner in _flattened(child)]
    return [error]


class CountingSource(ReplaySource):
    """A replay source that records its lifespan calls and nothing else."""

    def __init__(self) -> None:
        super().__init__()
        self.connects = 0
        self.closes = 0

    def connect(self) -> None:
        self.connects += 1
        super().connect()

    def close(self) -> None:
        self.closes += 1
        super().close()


@pytest.mark.anyio
async def test_the_lifespan_opens_the_source_once_and_never_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening the terminal once is a decision three files describe and nothing asserted.

    Deleting `source.connect()` from the lifespan left the suite green, and so did replacing
    the `finally` that closes it. `ReplaySource.connect` and `close` are no-ops, and the only
    source whose connect means anything is driven directly in `tests/test_mt5source.py` rather
    than through `build_server`, so no test ever watched the lifespan do its job.

    Per call it would matter: `mt5.initialize()` starts the terminal if it is not running,
    under a timeout documented as defaulting to 60000 milliseconds, which is long enough to
    make a host's first tool call look hung.
    """
    source = CountingSource()
    monkeypatch.setattr("quotez.server.make_source", lambda _config: source)
    server = build_server(ServerConfig())
    assert (source.connects, source.closes) == (0, 0), "building must open nothing"
    async with Client(server, raise_exceptions=True) as client:
        assert (source.connects, source.closes) == (1, 0), "the lifespan did not connect"
        await client.call_tool("get_quote", {"symbol": SYMBOL})
        await client.call_tool("list_symbols", {})
        assert source.connects == 1, "a tool call opened the source a second time"
    assert (source.connects, source.closes) == (1, 1), "the lifespan did not close"


@pytest.mark.anyio
async def test_the_lifespan_closes_the_source_even_when_the_session_blows_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This is the half the `finally` buys, and the half a happy-path assertion cannot see: a
    # close written after the yield instead of in a finally passes the test above and leaks the
    # terminal handle here.
    source = CountingSource()
    monkeypatch.setattr("quotez.server.make_source", lambda _config: source)
    collapse = RuntimeError("the host gave up mid session")
    with pytest.raises(BaseExceptionGroup) as caught:
        async with Client(build_server(ServerConfig()), raise_exceptions=True):
            raise collapse
    # anyio's task group repackages anything escaping the session body into nested groups, so
    # what was raised is identified rather than matched on the type that arrives.
    assert collapse in _flattened(caught.value)
    assert (source.connects, source.closes) == (1, 1)


# --------------------------------------------------------------------------------------
# Payload shapes


@pytest.mark.anyio
async def test_get_quote_structured_content_validates_against_the_quote_model(
    client: Client,
) -> None:
    result = await client.call_tool("get_quote", {"symbol": SYMBOL})
    assert result.is_error is False
    assert result.structured_content is not None
    quote = Quote.model_validate(result.structured_content)
    assert quote.symbol == SYMBOL
    assert quote.source == "replay"
    assert quote.synthetic is True
    assert quote.time.tzinfo is not None


@pytest.mark.anyio
async def test_get_bars_returns_an_unwrapped_model_not_a_result_envelope(
    client: Client,
) -> None:
    result = await client.call_tool("get_bars", {"symbol": SYMBOL, "timeframe": "H1", "count": 3})
    payload = result.structured_content
    assert payload is not None
    # A BaseModel return is used unwrapped; a scalar or a list would have arrived as
    # {"result": ...} instead.
    assert "result" not in payload
    assert set(payload) == {"symbol", "timeframe", "source", "synthetic", "count", "bars"}
    assert payload["count"] == 3
    assert len(payload["bars"]) == 3
    assert all(bar["spread"] is None for bar in payload["bars"])


@pytest.mark.anyio
async def test_symbol_info_returns_every_declared_field(client: Client) -> None:
    result = await client.call_tool("symbol_info", {"symbol": SYMBOL})
    payload = result.structured_content
    assert payload is not None
    assert set(payload) == set(SymbolSpec.model_fields)
    SymbolSpec.model_validate(payload)


@pytest.mark.anyio
async def test_the_bar_cap_appears_in_the_get_bars_input_schema(client: Client) -> None:
    schema = (await tool_map(client))["get_bars"].input_schema
    count = schema["properties"]["count"]
    # The bound is in the schema, not only in the handler, so the model can read it before
    # calling and the SDK enforces it before the handler runs.
    assert count["minimum"] == 1
    assert count["maximum"] == 5000
    assert count["default"] == 100
    assert schema["properties"]["timeframe"]["enum"] == [
        "M1",
        "M5",
        "M15",
        "M30",
        "H1",
        "H4",
        "D1",
    ]


@pytest.mark.anyio
async def test_the_group_filter_syntax_is_documented_in_the_input_schema(
    client: Client,
) -> None:
    schema = (await tool_map(client))["list_symbols"].input_schema
    description = schema["properties"]["group"]["description"]
    assert "!" in description
    assert "Inclusions must come before" in description


# --------------------------------------------------------------------------------------
# The resource


@pytest.mark.anyio
async def test_resources_list_contains_the_concrete_symbols_uri(client: Client) -> None:
    listed = (await client.list_resources()).resources
    by_uri = {str(resource.uri): resource for resource in listed}
    assert set(by_uri) == {"symbols://list"}
    universe = by_uri["symbols://list"]
    assert universe.name == "Instrument universe"
    assert universe.title == "Available instruments"
    assert universe.mime_type == "application/json"


@pytest.mark.anyio
async def test_no_resource_templates_are_registered(client: Client) -> None:
    # bars://{symbol}/{timeframe} was deliberately not built: it duplicates get_bars, and a
    # URI with placeholders leaves resources/list for resources/templates/list, which many
    # hosts surface poorly or not at all.
    assert (await client.list_resource_templates()).resource_templates == []


@pytest.mark.anyio
async def test_the_resource_payload_is_json_and_matches_list_symbols(client: Client) -> None:
    contents = (await client.read_resource("symbols://list")).contents
    assert len(contents) == 1
    assert contents[0].mime_type == "application/json"
    payload = json.loads(contents[0].text)
    listed = (await client.call_tool("list_symbols", {})).structured_content
    assert payload == listed
