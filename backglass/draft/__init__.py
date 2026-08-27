"""Drafting: everything the owner sends that the app helps write.

Two paths, one gate. `people/reachout.py` renders warm-keeping templates with no model
call, for a person the ledger has nothing on; `draft/reply.py` answers a real thread and
cannot avoid a model. Both pass their output through `draft/sweep.py` before it is shown,
so a draft's prose is held to one standard whichever path produced it, and both return a
record with the same `mailto()` / `as_text()` / `evidence` shape so the surfaces do not
have to learn two.

Nothing in this package writes a row, and nothing in it sends. A draft becomes evidence
when the owner sends it and the reply arrives back through the mail connector on the
ordinary path.
"""

from backglass.draft import reply, sweep

__all__ = ["reply", "sweep"]
