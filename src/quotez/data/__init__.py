"""Bundled replay fixtures, addressed as a package so `importlib.resources` can find them.

The CSVs and `symbols.json` live here rather than beside the repository root because the
package tree is the only thing a wheel is guaranteed to carry. This file exists so that
`importlib.resources.files("quotez.data")` resolves through the normal package loader on
every supported Python rather than through namespace package handling.

The contents are generated. `scripts/generate_replay_data.py` writes them, is run by hand,
and its output is committed. Nothing regenerates them at import time or test time.
"""

from __future__ import annotations
