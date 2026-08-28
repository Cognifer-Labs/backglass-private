"""`specs/state.md` must describe the answer `backglass state` actually gives.

CLAUDE.md sends every reader — a person at a terminal, and an assistant told to run it
first — to `backglass state` before trusting anything about this installation. Nothing
documented what it reports. The only way to know the surface was to read `state.py`, which
is the thing the command exists to save you from doing.

The failure this is built against is named in CLAUDE.md itself: *"an audit section
silently dropped by a merge."* A section that disappears takes its claims with it and
nothing anywhere says a question stopped being asked. A generated reference with a drift
test makes that impossible to do quietly — the same guarantee `specs/schema.sql` has, for
the same reason, and hand-syncing is not the fix because hand-syncing is what failed.

**Two probes, because one is not enough here and the difference is measurable.**

`collect()` on a fresh database emits 36 claims. `state.py` *declares* 41 — nine names are
written from more than one branch (`deployed/app` says "path exists" or names the path it
could not find) and five are only ever reached on a configured install (an exported vault,
a machine with an upstream). A reference built from one run would document 36 and be
silently blind to the rest, which is the same shape of hole it exists to close.

So the body is built from a live `collect()`, and `declared()` walks the module's own
`state.add(...)` call sites to say what the surface *is*. Every declared claim appears in
the document or a test fails; anything the fresh run could not reach is marked as such and
carries its source line instead of a value it does not have.

Regenerate with:  uv run python -m tests.test_state_reference
"""

from __future__ import annotations

import ast
import sqlite3
import tempfile
from pathlib import Path

from backglass import state as state_mod
from backglass.config import Settings
from backglass.db import connect, migrate

REFERENCE = Path(__file__).resolve().parent.parent / "specs" / "state.md"

HEADER = """\
# What `backglass state` reports

GENERATED from `backglass/state.py` — do not hand-edit. Add or move a claim, then run:

    uv run python -m tests.test_state_reference

`tests/test_state_reference.py` fails if this file and the module disagree, so a section
cannot be dropped by a merge without something saying so.

Every row is one claim and the derivation behind it. **The values are not here** — they
change on every run and belong to the command, not to a document. What belongs here is the
question each field answers and how you would check it yourself, which is the property
CLAUDE.md leans on when it says to prefer this command to inference.

A claim marked *unknown-capable* has a probe that can fail to run. When it does, `state`
says `unknown` and why, and never a zero — a confident answer assembled from a missing
input is the failure the whole command exists to prevent.

A claim marked *configured installs only* is real but unreachable on a fresh checkout, so
its derivation is read from the source rather than from a run.
"""

#: `errors` is not part of the surface: it appears only when a probe raised, its names are
#: whichever probes failed, and documenting it would be documenting the weather. `collect`
#: adds it dynamically for exactly that reason and it is the one call site `declared()`
#: cannot read.
EXCLUDED_SECTIONS = ("errors",)


def declared() -> dict[tuple[str, str], list[tuple[str | None, int]]]:
    """Every `state.add("section", "name", Claim(...))` the module contains.

    Static, so conditional branches count. A name written from two branches has two
    entries, which is how the document can say a claim has more than one derivation
    without a run ever taking both.

    `how` comes back as `None` where the argument is not a plain string — an f-string
    that interpolates a constant, usually — and the row falls back to naming its line.
    """
    tree = ast.parse(Path(state_mod.__file__).read_text())
    out: dict[tuple[str, str], list[tuple[str | None, int]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "state"):
            continue
        if len(node.args) < 3:
            continue
        section, name = node.args[0], node.args[1]
        if not (
            isinstance(section, ast.Constant) and isinstance(section.value, str)
            and isinstance(name, ast.Constant) and isinstance(name.value, str)
        ):
            continue  # the `errors` catch-all; see EXCLUDED_SECTIONS
        how: str | None = None
        claim = node.args[2]
        if isinstance(claim, ast.Call) and len(claim.args) >= 2:
            arg = claim.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                how = arg.value
        out.setdefault((section.value, name.value), []).append((how, node.lineno))
    return out


def observed(conn: sqlite3.Connection, settings: Settings) -> state_mod.State:
    return state_mod.collect(conn, settings)


def render(state: state_mod.State, checks: list[state_mod.Verdict]) -> str:
    """The document. Sections in the order `collect` builds them, claims in theirs.

    Pure, and it takes the verdicts rather than computing them. `verdicts()` reads claim
    *values* — it is the half of `state` that judges them — so a renderer that called it
    could not be handed a state with the values changed, which is exactly what
    `test_the_document_carries_no_values` needs to do to mean anything.
    """
    sites = declared()
    seen: set[tuple[str, str]] = set()
    lines = [HEADER]

    for section, claims in state.sections.items():
        if section in EXCLUDED_SECTIONS:
            continue
        lines.append(f"\n## `{section}`\n")
        lines.append("| claim | derivation | notes |")
        lines.append("| --- | --- | --- |")
        for name, claim in claims.items():
            seen.add((section, name))
            notes = []
            if claim.unknown is not None:
                notes.append("unknown-capable")
            if len(sites.get((section, name), [])) > 1:
                notes.append(f"{len(sites[(section, name)])} derivations")
            lines.append(
                f"| `{name}` | {_cell(claim.how)} | {', '.join(notes) or '—'} |"
            )

    missing = sorted(k for k in sites if k not in seen and k[0] not in EXCLUDED_SECTIONS)
    if missing:
        lines.append("\n## Configured installs only\n")
        lines.append(
            "Claims a fresh checkout never reaches — an exported vault, a machine with a\n"
            "configured upstream. Named here so the surface is complete rather than\n"
            "complete-as-far-as-this-machine-got.\n"
        )
        lines.append("| claim | derivation | source |")
        lines.append("| --- | --- | --- |")
        for section, name in missing:
            how, lineno = sites[(section, name)][0]
            where = f"`state.py:{lineno}`"
            lines.append(f"| `{section}.{name}` | {_cell(how) if how else '—'} | {where} |")

    lines.append("\n## Verdicts\n")
    lines.append(
        "What `state` *judges*, rather than reports. Forty claims with no verdict left\n"
        "every reader to know which numbers were bad news; these are the ones that say so\n"
        "themselves, and each carries the remedy for its own failure.\n"
    )
    lines.append("| verdict |")
    lines.append("| --- |")
    for verdict in checks:
        lines.append(f"| {verdict.name} |")

    return "\n".join(lines) + "\n"


def _cell(text: str | None) -> str:
    """A derivation inside a table cell. Pipes would end the column; backticks would
    fight the SQL that is already quoted inside several of them."""
    if not text:
        return "—"
    return text.replace("|", "\\|").replace("\n", " ")


def build(tmp_path: Path, settings: Settings) -> str:
    conn = connect(tmp_path / "reference.db")
    try:
        migrate(conn)
        state = observed(conn, settings)
        return render(state, state_mod.verdicts(state, conn, settings))
    finally:
        conn.close()


def test_the_reference_matches_state_py(tmp_path: Path, settings: Settings) -> None:
    expected = build(tmp_path, settings)
    actual = REFERENCE.read_text()
    assert actual == expected, (
        "specs/state.md is out of date with backglass/state.py. "
        "Regenerate it: uv run python -m tests.test_state_reference"
    )


def test_every_declared_claim_is_documented() -> None:
    """The guard the drift test alone does not give.

    A claim behind a branch a fresh run never takes would be missing from a document
    generated purely by running the thing, and its absence would look exactly like a
    claim that had been deleted. So the document is checked against what the module
    *declares*, not against what one machine happened to reach.
    """
    body = REFERENCE.read_text()
    undocumented = [
        f"{section}.{name}"
        for (section, name) in declared()
        if section not in EXCLUDED_SECTIONS
        and f"`{name}`" not in body
        and f"`{section}.{name}`" not in body
    ]
    assert undocumented == [], (
        f"state.py declares claims specs/state.md does not name: {undocumented}. "
        "Regenerate it: uv run python -m tests.test_state_reference"
    )


def test_a_dropped_section_cannot_pass_quietly(tmp_path: Path, settings: Settings) -> None:
    """The failure CLAUDE.md records: an audit section dropped by a merge, silently.

    Simulated rather than asserted in prose — the document is rebuilt from a state with
    one section removed, and the result has to differ from what is committed. A reference
    that still matched would be a reference that cannot see a deletion.
    """
    conn = connect(tmp_path / "dropped.db")
    try:
        migrate(conn)
        state = observed(conn, settings)
        checks = state_mod.verdicts(state, conn, settings)
        assert "ledger" in state.sections
        del state.sections["ledger"]
        assert render(state, checks) != REFERENCE.read_text()
    finally:
        conn.close()


def test_the_document_carries_no_values(tmp_path: Path, settings: Settings) -> None:
    """Derivations, never readings.

    A document that pasted this morning's counts would be wrong by lunchtime and would
    teach whoever read it to trust a number the command exists to compute fresh. It is
    also what keeps the drift test meaningful: a reference that changed every run would
    be regenerated on reflex and stop being read.
    """
    conn = connect(tmp_path / "values.db")
    try:
        migrate(conn)
        state = observed(conn, settings)
        checks = state_mod.verdicts(state, conn, settings)
        first = render(state, checks)
        for claims in state.sections.values():
            for claim in claims.values():
                claim.value = "A VALUE THAT WOULD BE WRONG BY LUNCHTIME"
        assert render(state, checks) == first
    finally:
        conn.close()


def test_no_configuration_produces_a_claim_the_document_does_not_name(
    tmp_path: Path, settings: Settings
) -> None:
    """The reference is built from a default configuration, so this is the guard on that.

    A machine with `VAULT_EXPORT_PATH` set reaches five claims a fresh checkout never
    does. Building the document from whichever machine ran the updater would make its
    content depend on the directory somebody stood in — and would fail CI for whoever
    regenerated it second. So it is built from the module plus a default config, and the
    unreachable claims are read statically into their own section.

    Which is only sound if *no* configuration can produce a claim the document misses.
    That is what this asserts, with the configuration that actually differs.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "STATE.md").write_text("---\ngenerated_at: 2026-08-27T00:00:00Z\n---\n")
    configured = settings.model_copy(update={"vault_export_path": vault})

    conn = connect(tmp_path / "configured.db")
    try:
        migrate(conn)
        emitted = {
            (section, name)
            for section, claims in observed(conn, configured).sections.items()
            for name in claims
            if section not in EXCLUDED_SECTIONS
        }
    finally:
        conn.close()

    body = REFERENCE.read_text()
    missing = sorted(
        f"{section}.{name}"
        for section, name in emitted
        if f"`{name}`" not in body and f"`{section}.{name}`" not in body
    )
    assert missing == [], (
        f"a configured install reports claims specs/state.md never names: {missing}"
    )


if __name__ == "__main__":  # regeneration entry point, named in the header above
    # The same insulated settings the tests get, and NOT `get_settings()`. The owner's
    # `.env` sets VAULT_EXPORT_PATH, so regenerating from the main checkout would document
    # a vault-configured surface while regenerating from a worktree with no `.env`
    # documents an unconfigured one — a reference whose content depends on which directory
    # you stood in, failing CI for whoever regenerated it second. The document describes
    # the module, so it is built from the module and a default configuration.
    from tests.conftest import build_settings

    with tempfile.TemporaryDirectory() as tmp:
        REFERENCE.write_text(build(Path(tmp), build_settings(Path(tmp))))
    print(f"wrote {REFERENCE}")
