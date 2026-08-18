"""MetaTrader 5's symbol group filter, reimplemented for sources that have no terminal.

`Mt5Source` hands `group=` straight to `symbols_get` and lets the terminal do the work.
The replay source has no terminal, and a filter that behaves differently depending on
which source is configured is worse than no filter at all, so the syntax is implemented
once here and both sources agree.

Syntax, per the MetaTrader 5 documentation for `symbols_get`: `*` is a wildcard, several
conditions are separated by commas, and `!` negates a condition. Conditions apply in
order, each one adding to or removing from the selection, which is why the documentation
says inclusions must come first. `"*, !*USD*"` is everything except the USD pairs, while
`"!*USD*, *"` puts them back and matches everything. That is not a quirk to work around,
it is the documented behaviour, and silently reordering the conditions would make this
filter disagree with the terminal.

Matching is case sensitive, like the symbol names themselves.
"""

from __future__ import annotations

from quotez.errors import InvalidRequest

__all__ = ["match_group"]


def match_group(name: str, group: str | None) -> bool:
    """True when `name` survives the `group` filter. `None` means no filter at all."""
    if group is None:
        return True
    selected = False
    for condition in _conditions(group):
        negated = condition.startswith("!")
        pattern = condition[1:] if negated else condition
        if _matches(name, pattern):
            selected = not negated
    return selected


def _conditions(group: str) -> list[str]:
    conditions = [part.strip() for part in group.split(",")]
    for condition in conditions:
        if not condition or condition == "!":
            raise InvalidRequest(
                f"Group filter {group!r} has an empty condition. Use comma separated patterns "
                'such as "*FX*" or "*, !*USD*".'
            )
    return conditions


def _matches(name: str, pattern: str) -> bool:
    # Deliberately not fnmatch: MetaTrader documents `*` only, and fnmatch would also
    # honour `?` and `[...]`, so a broker symbol such as [SP500] would be read as a
    # character class instead of a name.
    parts = pattern.split("*")
    if len(parts) == 1:
        return name == pattern
    head, tail = parts[0], parts[-1]
    if not name.startswith(head) or not name.endswith(tail):
        return False
    position = len(head)
    limit = len(name) - len(tail)
    if limit < position:
        return False
    for middle in parts[1:-1]:
        found = name.find(middle, position, limit)
        if found < 0:
            return False
        position = found + len(middle)
    return True
