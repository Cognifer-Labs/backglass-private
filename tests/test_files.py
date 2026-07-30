"""The drop folder connector. backglass/connectors/files.py.

A folder the owner saves documents into. The rules that are its own — what counts as a
document, what a dropped .docx does, what a hidden file does — plus the two every local
connector shares: the docs/08 boundary runs before persistence, and the mtime watermark
keeps full precision so the second run reads nothing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backglass.connectors.base import Connector
from backglass.connectors.boundary import Boundary
from backglass.connectors.files import FilesConnector


@pytest.fixture
def enforcing() -> Boundary:
    return Boundary(mode="exclude", deny_domains=["clientexample.gov"])


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    root = tmp_path / "drop"
    root.mkdir()
    return root


def _write(path: Path, text: str, *, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_it_satisfies_the_connector_protocol(folder: Path, enforcing: Boundary) -> None:
    connector = FilesConnector(folder_path=folder, boundary=enforcing)
    assert isinstance(connector, Connector)
    assert connector.name == "files"


def test_txt_and_md_become_source_items(folder: Path, enforcing: Boundary) -> None:
    _write(folder / "letter.txt", "Send Dana the signed scope by Friday.")
    _write(folder / "meetings" / "kickoff.md", "# Kickoff\n\nI owe Ravi the deck.\n")

    connector = FilesConnector(folder_path=folder, boundary=enforcing)
    items = sorted(connector.fetch(None), key=lambda item: item.external_id)

    assert len(items) == 2
    assert [item.source for item in items] == ["files", "files"]
    # The id is the readable relative path, so the owner can find the file it came from.
    assert [item.external_id for item in items] == ["letter.txt", "meetings/kickoff.md"]
    assert [item.title for item in items] == ["letter.txt", "kickoff.md"]
    assert items[0].author is None
    assert "signed scope" in str(items[0].body_text)
    assert "I owe Ravi" in str(items[1].body_text)
    assert all(item.content_hash for item in items)
    assert connector.excluded_by_rule == {}


def test_a_files_frontmatter_date_beats_its_mtime(folder: Path, enforcing: Boundary) -> None:
    """CLAUDE.md rule 4. A note written on the 10th and dropped in today must resolve
    "by Friday" against the 10th, not against the day it was dropped."""
    _write(
        folder / "note.md",
        "---\ndate: 2026-07-10\n---\n\nSend the scope by Friday.\n",
        mtime=1_785_000_000.0,  # 2026-07-25, well after the date it claims
    )
    connector = FilesConnector(folder_path=folder, boundary=enforcing)
    item = next(iter(connector.fetch(None)))

    assert item.occurred_at.startswith("2026-07-10")
    assert "---" not in str(item.body_text), "frontmatter is stripped"


def test_the_mtime_is_the_anchor_when_nothing_dates_itself(
    folder: Path, enforcing: Boundary
) -> None:
    _write(folder / "scan.txt", "Received the signed lease.", mtime=1_785_000_000.0)
    connector = FilesConnector(folder_path=folder, boundary=enforcing)
    item = next(iter(connector.fetch(None)))

    assert item.occurred_at.startswith("2026-07-25")
    assert item.occurred_at.endswith("+00:00"), "stored in UTC"


def test_an_unsupported_extension_is_counted_never_stored(
    folder: Path, enforcing: Boundary
) -> None:
    """No dependency in this repo extracts text from a .pdf or a .docx. Silently ignoring
    a file the owner deliberately dropped in is the failure docs/11 §8 calls dangerous, so
    it is counted and surfaced instead."""
    _write(folder / "contract.pdf", "%PDF-1.7 not really")
    _write(folder / "proposal.docx", "PK binary-ish")
    _write(folder / "keep.txt", "This one is readable.")

    connector = FilesConnector(folder_path=folder, boundary=enforcing)
    items = list(connector.fetch(None))

    assert [item.external_id for item in items] == ["keep.txt"]
    assert connector.excluded_by_rule == {"unsupported": 2}
    # Format is not a privacy exclusion; docs/08 D5's tally stays the boundary's.
    assert connector.excluded == 0


def test_hidden_files_and_folders_are_skipped(folder: Path, enforcing: Boundary) -> None:
    _write(folder / ".DS_Store", "junk")
    _write(folder / ".hidden.md", "not a document")
    _write(folder / ".sync" / "cache.txt", "sync noise")
    _write(folder / "real.txt", "A real document.")

    connector = FilesConnector(folder_path=folder, boundary=enforcing)
    items = list(connector.fetch(None))

    assert [item.external_id for item in items] == ["real.txt"]
    # Skipped, not "excluded" — a hidden file was never a candidate document.
    assert connector.excluded_by_rule == {}


def test_a_dropped_file_quoting_a_client_is_excluded(folder: Path, enforcing: Boundary) -> None:
    """docs/08 D1 does not care which connector is persisting. A printed thread dropped
    into the folder is still client correspondence."""
    _write(folder / "printed.txt", "From: dana@clientexample.gov\n\nthe WIC numbers\n")
    _write(folder / "mine.txt", "My own notes.")

    connector = FilesConnector(folder_path=folder, boundary=enforcing)
    items = list(connector.fetch(None))

    assert [item.external_id for item in items] == ["mine.txt"]
    assert connector.excluded == 1
    assert connector.excluded_by_rule == {"clientexample.gov": 1}


def test_the_watermark_keeps_full_precision(folder: Path, enforcing: Boundary) -> None:
    """tasks/lessons.md, 2026-07-30. Truncating the watermark to whole seconds makes a
    file saved at 12:00:00.5 permanently newer than the cursor, so every run re-reads
    everything — invisibly, because content_hash means it still writes nothing. So assert
    the second run FETCHES nothing, not that it writes nothing."""
    _write(folder / "a.txt", "First document.", mtime=1_785_000_000.5)
    _write(folder / "b.md", "Second document.", mtime=1_785_000_123.75)

    first = FilesConnector(folder_path=folder, boundary=enforcing)
    assert len(list(first.fetch(None))) == 2
    cursor = first.cursor
    assert cursor
    assert cursor.startswith("2026-07-25T"), cursor
    assert ".75" in cursor, "sub-second precision survives into the cursor"

    second = FilesConnector(folder_path=folder, boundary=enforcing)
    assert list(second.fetch(cursor)) == [], "unchanged files are not re-fetched"

    _write(folder / "c.txt", "Dropped later.", mtime=1_785_000_200.25)
    third = FilesConnector(folder_path=folder, boundary=enforcing)
    assert [item.external_id for item in third.fetch(cursor)] == ["c.txt"]


def test_a_missing_folder_is_a_visible_failure(tmp_path: Path, enforcing: Boundary) -> None:
    """Reporting zero documents would look like an empty folder, which is exactly the
    silent failure docs/11 §8 warns about."""
    connector = FilesConnector(folder_path=tmp_path / "nope", boundary=enforcing)
    health = connector.health()

    assert health.ok is False
    assert "not found" in str(health.detail)
    assert str(tmp_path / "nope") in str(health.detail)
    with pytest.raises(FileNotFoundError):
        list(connector.fetch(None))
