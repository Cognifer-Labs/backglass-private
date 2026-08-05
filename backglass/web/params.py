"""Shared path-parameter types.

One annotation, because the alternative is a rule applied at twenty call sites and
therefore at nineteen.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Path

#: The largest integer SQLite stores. Python's int is unbounded and FastAPI's `int`
#: converter inherits that, so `/people/999999999999999999999999999999` parsed fine and
#: then died inside the driver with `OverflowError: Python int too large to convert to
#: SQLite INTEGER` — a 500 for a URL whose honest answer is "no such row". Bounding the
#: parameter turns every one of those into a 422 before a query is ever built.
#:
#: `ge=1` because these are rowids: SQLite hands them out from 1 upward, so 0 and the
#: negatives cannot name anything either.
SQLITE_MAX_INT = 2**63 - 1

RowId = Annotated[int, Path(ge=1, le=SQLITE_MAX_INT)]
