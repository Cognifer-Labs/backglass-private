"""The vault Backglass writes, and the loop it must not close.

Two of these tests are the whole safety argument. `test_generated_notes_are_not_ingested`
is the one that keeps the record from feeding on its own output once the export path and
the ingest path point at the same folder; `test_inbox_note_is_ingested` is its other
half, because a guard that skips everything is indistinguishable from a broken connector.

The rest pin the three rules the vault could quietly break: provenance on every claim
(rule 1), low-confidence extractions kept out of the settled list (rule 2), and a second
run that writes nothing (rule 3).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from backglass import state as state_mod
from backglass import vault
from backglass.config import Settings
from backglass.connectors.boundary import Boundary
from backglass.connectors.notes import NotesConnector
from backglass.db import now_iso
from backglass.ledger import USER_ID

NOW = datetime(2026, 8, 23, 9, 30)


@pytest.fixture
def snapshot() -> tuple[state_mod.State, list[state_mod.Verdict]]:
    """A hand-built `backglass state` reading.

    Injected rather than collected because `state.collect` shells out to git and hashes
    the installed app: real inputs, and neither of them the thing under test here. The
    export takes the snapshot as a parameter for exactly this reason.
    """
    built = state_mod.State()
    built.add("ledger", "source_items", state_mod.Claim(value=3, how="SELECT COUNT(*)"))
    built.add(
        "deployed",
        "matches_source",
        state_mod.Claim(value=None, unknown="no app installed", how="path exists"),
    )
    return built, [
        state_mod.Verdict(name="schema is current", ok=True, detail="32 applied"),
        state_mod.Verdict(
            name="installed app matches this checkout",
            ok=False,
            detail="7 stale",
            remedy="./desktop/build-sidecar.sh",
        ),
    ]


def a_document(conn: sqlite3.Connection, title: str = "a letter") -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'files', ?, ?, '2026-08-01T09:15:00-07:00', NULL, ?, ?, '{}', ?, 'keep')",
        (USER_ID, f"ext-{title}", now_iso(), title, "the body", f"hash-{title}"),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def a_fact(
    conn: sqlite3.Connection,
    subject: str,
    key: str,
    value: str,
    *,
    source_item_id: int | None = None,
    status: str = "active",
    confidence: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO fact (user_id, subject, key, value, note, source, source_item_id,"
        " status, created_at, confidence) VALUES (?, ?, ?, ?, NULL, 'extraction', ?, ?, ?, ?)",
        (USER_ID, subject, key, value, source_item_id, status, now_iso(), confidence),
    )


def a_commitment(
    conn: sqlite3.Connection, what: str, *, confidence: float, source_item_id: int
) -> None:
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, confidence, status,"
        " source_item_id, created_at) VALUES (?, 'i_owe', ?, '2026-09-01', ?, 'open', ?, ?)",
        (USER_ID, what, confidence, source_item_id, now_iso()),
    )


def rendered(conn: sqlite3.Connection, settings: Settings, snapshot: object) -> dict[str, str]:
    notes = vault.render(conn, settings, now=NOW, snapshot=snapshot)  # type: ignore[arg-type]
    return {note.path: note.body for note in notes}


# ── the loop ─────────────────────────────────────────────────────────────────


def test_generated_notes_are_not_ingested(
    tmp_path: Path, conn: sqlite3.Connection, settings: Settings, boundary: Boundary, snapshot
) -> None:
    """The whole point. Export into a vault, then read that vault: nothing comes back.

    Without the frontmatter marker this connector would hand every generated note to
    triage as a new `source_item` — permanently, since raw items are never deleted — and
    the next export would render facts extracted from the last one.
    """
    a_fact(conn, "housing", "dorm", "Barrett", source_item_id=a_document(conn))
    root = tmp_path / "vault"
    report = vault.export(conn, settings, root=root, now=NOW, snapshot=snapshot)
    assert report.written, "nothing was exported, so this proves nothing"

    connector = NotesConnector(vault_path=root, boundary=boundary)
    items = list(connector.fetch(None))

    assert items == []
    assert connector.generated == len(report.written)
    # Counted apart from boundary exclusions, which mean something else entirely.
    assert connector.excluded == 0


def test_inbox_note_is_ingested(
    tmp_path: Path, conn: sqlite3.Connection, settings: Settings, boundary: Boundary, snapshot
) -> None:
    """The other half: the owner's own note in the same vault still flows in."""
    root = tmp_path / "vault"
    vault.export(conn, settings, root=root, now=NOW, snapshot=snapshot)
    (root / "Inbox" / "housing.md").write_text(
        "---\ndate: 2026-08-20\n---\n\nPay the housing deposit by Friday.\n",
        encoding="utf-8",
    )

    items = list(NotesConnector(vault_path=root, boundary=boundary).fetch(None))

    assert [item.title for item in items] == ["housing"]
    assert "housing deposit" in items[0].body_text


# ── the rules ────────────────────────────────────────────────────────────────


def test_second_export_writes_nothing(
    tmp_path: Path, conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    """Rule 3, and the reason the notes connector is not handed a fresh mtime each run."""
    a_fact(conn, "identity", "asu id", "1216...", source_item_id=a_document(conn))
    root = tmp_path / "vault"

    first = vault.export(conn, settings, root=root, now=NOW, snapshot=snapshot)
    second = vault.export(conn, settings, root=root, now=NOW, snapshot=snapshot)

    assert first.written
    assert second.written == []
    assert len(second.unchanged) == first.total


def test_every_note_says_it_is_generated(
    conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    for path, body in rendered(conn, settings, snapshot).items():
        assert body.startswith(f"---\n{vault.MARK_KEY}: {vault.MARK_VALUE}\n"), path


def test_a_fact_links_to_the_document_it_came_from(
    conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    """Rule 1. And a fact with no document says so rather than implying one."""
    source_id = a_document(conn)
    a_fact(conn, "housing", "dorm", "Barrett", source_item_id=source_id)
    a_fact(conn, "identity", "name", "Dharsan")

    notes = rendered(conn, settings, snapshot)

    assert f"/source/{source_id}" in notes["Facts/housing.md"]
    assert "_no source_" in notes["Facts/identity.md"]


def test_low_confidence_commitments_are_named_unconfirmed(
    conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    """Rule 2. A review-queue item is on the page, under its own heading, never above it."""
    source_id = a_document(conn)
    a_commitment(conn, "settled thing", confidence=0.95, source_item_id=source_id)
    a_commitment(conn, "doubtful thing", confidence=0.20, source_item_id=source_id)

    body = rendered(conn, settings, snapshot)["Commitments.md"]
    settled, _, unconfirmed = body.partition("## Unconfirmed")

    assert "settled thing" in settled
    assert "doubtful thing" not in settled
    assert "doubtful thing" in unconfirmed


def test_proposed_facts_wait_rather_than_read_as_facts(
    conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    a_fact(conn, "housing", "dorm", "Barrett", source_item_id=a_document(conn))
    a_fact(conn, "housing", "roommate", "unclear", status="proposed", confidence=0.4)

    me = rendered(conn, settings, snapshot)["Me.md"]
    active, _, waiting = me.partition("## Waiting on you")

    assert "Barrett" in active
    assert "roommate" not in active
    assert "roommate" in waiting


def test_state_note_carries_the_checks_and_the_unknowns(
    conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    """A probe that could not run says so. Never a zero, never silence."""
    body = rendered(conn, settings, snapshot)["STATE.md"]

    assert "schema is current" in body
    assert "./desktop/build-sidecar.sh" in body
    assert "_unknown_ — no app installed" in body
    assert "2026-08-23 09:30" in body


def test_a_vault_that_cannot_be_written_degrades(
    tmp_path: Path, conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    """Rule 5. A read-only vault is a degradation, not an exception out of the sync."""
    root = tmp_path / "locked"
    root.mkdir()
    root.chmod(0o500)
    try:
        report = vault.export(conn, settings, root=root, now=NOW, snapshot=snapshot)
    finally:
        root.chmod(0o700)

    assert report.failed
    assert report.written == []


def test_a_name_the_filesystem_would_refuse_is_made_safe() -> None:
    assert vault.safe_name("CHM 113 / lab") == "CHM 113 lab"
    assert vault.safe_name("???") == "untitled"


# ── the graph ────────────────────────────────────────────────────────────────
#
# A vault of unlinked notes is a folder of text files; the backlink pane and the graph are
# the only reasons to put a knowledge base in Obsidian rather than in the dashboard. So
# these pin what gets linked — and, just as much, what does not, because a confident link
# to a note that was never written is worse than plain text.


def a_person(conn: sqlite3.Connection, name: str, *, role: str = "advisor") -> int:
    conn.execute(
        "INSERT INTO entity (user_id, kind, canonical_name, role) VALUES (?, 'person', ?, ?)",
        (USER_ID, name, role),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def test_a_commitment_links_to_the_person_it_is_with(
    conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    entity_id = a_person(conn, "Rachel Espericueta")
    source_id = a_document(conn)
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, confidence, status,"
        " source_item_id, counterparty_entity_id, created_at)"
        " VALUES (?, 'i_owe', 'send the form', '2026-09-01', 0.95, 'open', ?, ?, ?)",
        (USER_ID, source_id, entity_id, now_iso()),
    )

    notes = rendered(conn, settings, snapshot)

    assert "[[People/Rachel Espericueta|Rachel Espericueta]]" in notes["Commitments.md"]
    assert "People/Rachel Espericueta.md" in notes


def test_a_full_name_in_a_fact_becomes_a_link_and_a_bare_word_does_not(
    conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    a_person(conn, "Rachel Espericueta")
    a_person(conn, "Will")
    a_fact(
        conn,
        "education",
        "advisor",
        "Rachel Espericueta is the advisor; she will confirm",
        source_item_id=a_document(conn),
    )

    body = rendered(conn, settings, snapshot)["Facts/education.md"]

    assert "[[People/Rachel Espericueta|Rachel Espericueta]]" in body
    # "Will" is a person in the ledger and also the word in "she will confirm". A
    # one-token name is never linked, because the false positives are every sentence.
    assert "[[People/Will" not in body


def test_a_course_code_links_only_when_the_class_note_exists(
    conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    """No semester is loaded in this fixture, so nothing may claim CHM 113 has a note."""
    a_fact(
        conn, "education", "lab", "CHM 113 lab meets Thursday", source_item_id=a_document(conn)
    )

    body = rendered(conn, settings, snapshot)["Facts/education.md"]

    assert "CHM 113 lab meets Thursday" in body
    assert "[[Classes/CHM 113" not in body


def test_text_that_is_already_a_link_is_left_alone() -> None:
    index = vault.Index(people={"rachel espericueta": "People/Rachel Espericueta.md"})
    text = "see [[People/Rachel Espericueta]] about it"

    assert vault._mentions(text, index) == text


def test_a_person_with_no_note_stays_plain_text() -> None:
    assert vault._mentions("Rachel Espericueta owes a form", vault.Index()) == (
        "Rachel Espericueta owes a form"
    )


# ── pruning ──────────────────────────────────────────────────────────────────
#
# The export only ever grew until 2026-08-23, and what it grew was stale claims: a person
# whose last open commitment closed dropped out of the render, and their note stayed on
# disk saying they owed something. These pin the two conditions that make deleting safe.


def test_a_note_the_ledger_no_longer_produces_is_removed(
    tmp_path: Path, conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    entity_id = a_person(conn, "Rachel Espericueta")
    root = tmp_path / "vault"
    vault.export(conn, settings, root=root, now=NOW, snapshot=snapshot)
    assert (root / "People" / "Rachel Espericueta.md").exists()

    # She earned the note by having a role. Take it away and she is a bare name again.
    conn.execute("UPDATE entity SET role = NULL WHERE id = ?", (entity_id,))
    report = vault.export(conn, settings, root=root, now=NOW, snapshot=snapshot)

    assert "People/Rachel Espericueta.md" in report.removed
    assert not (root / "People" / "Rachel Espericueta.md").exists()


def test_a_note_the_owner_wrote_is_never_removed(
    tmp_path: Path, conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    """Two files in a folder the export owns; only the generated one may be touched."""
    root = tmp_path / "vault"
    vault.export(conn, settings, root=root, now=NOW, snapshot=snapshot)
    mine = root / "Facts" / "my own thinking.md"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text("no frontmatter, no marker, my file\n", encoding="utf-8")

    report = vault.export(conn, settings, root=root, now=NOW, snapshot=snapshot)

    assert report.removed == []
    assert mine.exists()


def test_a_generated_note_moved_out_of_the_way_is_left_alone(
    tmp_path: Path, conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    """Dragging a note into `Archive/` is a deliberate act; the prune does not undo it."""
    a_fact(conn, "housing", "dorm", "Barrett", source_item_id=a_document(conn))
    root = tmp_path / "vault"
    vault.export(conn, settings, root=root, now=NOW, snapshot=snapshot)
    archive = root / "Archive"
    archive.mkdir()
    moved = archive / "old housing.md"
    moved.write_text((root / "Facts" / "housing.md").read_text(encoding="utf-8"), "utf-8")

    report = vault.export(conn, settings, root=root, now=NOW, snapshot=snapshot)

    assert report.removed == []
    assert moved.exists()


def test_a_dry_run_names_what_it_would_delete_and_deletes_nothing(
    tmp_path: Path, conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    entity_id = a_person(conn, "Rachel Espericueta")
    root = tmp_path / "vault"
    vault.export(conn, settings, root=root, now=NOW, snapshot=snapshot)
    conn.execute("UPDATE entity SET role = NULL WHERE id = ?", (entity_id,))

    report = vault.export(conn, settings, root=root, now=NOW, dry_run=True, snapshot=snapshot)

    assert report.removed == ["People/Rachel Espericueta.md"]
    assert (root / "People" / "Rachel Espericueta.md").exists()


# ── the evidence pile ────────────────────────────────────────────────────────


def a_drop_folder_document(conn: sqlite3.Connection, title: str) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'files', ?, ?, '2026-08-02T09:15:00-07:00', NULL, ?, 'text', '{}', ?,"
        " 'keep')",
        (USER_ID, f"file-{title}", now_iso(), title, f"h-file-{title}"),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def test_a_document_says_what_it_produced(
    conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    source_id = a_drop_folder_document(conn, "housing contract")
    a_fact(conn, "housing", "dorm", "Barrett", source_item_id=source_id)

    body = rendered(conn, settings, snapshot)["Documents.md"]

    assert "## housing contract" in body
    assert f"/source/{source_id}" in body
    assert "[[Facts/housing|housing]]" in body
    assert "**dorm** — Barrett" in body


def test_a_document_that_produced_nothing_says_so(
    conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    """A folder listing cannot tell those two apart, which is why this note exists."""
    a_drop_folder_document(conn, "a scanned flyer")

    body = rendered(conn, settings, snapshot)["Documents.md"]

    assert "## a scanned flyer" in body
    assert "stated nothing the ledger keeps" in body


def test_the_documents_note_points_at_search_rather_than_faking_it(
    conn: sqlite3.Connection, settings: Settings, snapshot
) -> None:
    """Retrieval is a command over the whole corpus; a file can only list part of it."""
    a_drop_folder_document(conn, "a letter")

    body = rendered(conn, settings, snapshot)["Documents.md"]

    assert "backglass search find" in body
    assert "Searchable:" in body


def test_every_occurrence_is_linked_and_none_is_nested(conn: sqlite3.Connection) -> None:
    """One rule for both kinds of mention, and no link inside a link.

    The second clause is the one that would produce markup Obsidian cannot parse: the
    course pass runs first and leaves an alias behind, so a name matching inside that
    alias must be left alone.
    """
    index = vault.Index(
        people={"beatriz smith": "People/Beatriz Smith.md"},
        classes={"CHM 113": "Classes/CHM 113.md"},
    )

    linked = vault._mentions("CHM 113 with Beatriz Smith; ask Beatriz Smith again", index)

    assert linked.count("[[People/Beatriz Smith|") == 2
    assert linked.count("[[Classes/CHM 113|") == 1
    assert "[[[" not in linked and "]]]" not in linked


class TestStatus:
    """`vault.status` — what /memory says about the folder it links into.

    The per-lane `obsidian://` links on that page have always been optimistic: they point
    into a vault whose last export the page never stated, so a link into notes written
    three weeks ago looked identical to one written this morning.
    """

    def test_no_vault_configured_is_not_an_empty_vault(self, settings: Settings) -> None:
        """`root is None` and `notes is None` are different facts from `notes == 0`, and
        the page prints a different sentence for each."""
        status = vault.status(settings.model_copy(update={"vault_export_path": None}))

        assert status.root is None
        assert status.notes is None

    def test_a_configured_but_absent_root_reports_the_path_and_no_count(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        missing = tmp_path / "never-exported"
        status = vault.status(settings.model_copy(update={"vault_export_path": missing}))

        assert status.root == missing
        assert status.notes is None

    def test_it_counts_notes_and_reads_the_export_time_from_state_md(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        root = tmp_path / "Vault"
        (root / "Facts").mkdir(parents=True)
        (root / "Facts" / "housing.md").write_text("---\nbackglass: generated\n---\n")
        (root / "STATE.md").write_text(
            "---\nbackglass: generated\ntype: state\n"
            "generated_at: 2026-08-25T12:31:51-07:00\n---\nbody\n"
        )
        # Obsidian's own config directory is not the owner's writing and is not a note.
        (root / ".obsidian").mkdir()
        (root / ".obsidian" / "workspace.md").write_text("noise")

        status = vault.status(settings.model_copy(update={"vault_export_path": root}))

        assert status.notes == 2
        assert status.exported_at == "2026-08-25T12:31:51-07:00"

    def test_a_state_note_without_a_timestamp_is_unknown_not_wrong(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        """Written by something other than the export. Saying "unknown" is the honest
        answer; inventing the file's mtime would be a claim nobody made."""
        root = tmp_path / "Vault"
        root.mkdir()
        (root / "STATE.md").write_text("no frontmatter here\n")

        assert vault.status(
            settings.model_copy(update={"vault_export_path": root})
        ).exported_at is None

    def test_it_says_when_the_vault_is_also_an_ingest_source(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        """The loop the `backglass: generated` marker exists to stop. The page states it
        rather than leaving the owner to discover that the folder is read as well as
        written."""
        root = tmp_path / "Vault"
        root.mkdir()
        both = settings.model_copy(
            update={"vault_export_path": root, "obsidian_vault_path": root}
        )
        one_way = settings.model_copy(
            update={"vault_export_path": root, "obsidian_vault_path": None}
        )

        assert vault.status(both).also_ingested is True
        assert vault.status(one_way).also_ingested is False
