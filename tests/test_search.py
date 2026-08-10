"""Retrieval over the document pile. Owner's ruling 2026-08-10; docs/02 revised.

The concession is bounded and the bounds are the reason it is safe, so they are what these
tests pin: retrieval is additive and never load-bearing, two models' vectors are never
ranked against each other, and a partial index says so rather than returning a confident
subset.

No live endpoint — docs/10 §Testing forbids it, and a stub is also the only way to assert
on ranking, since a real embedder's scores are not knowable in advance.
"""

from __future__ import annotations

import sqlite3

import pytest

from backglass import search
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID


@pytest.fixture
def sett(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"model_base_url": "http://127.0.0.1:11434/v1", "embedding_model": "stub-embed"}
    )


def a_document(conn: sqlite3.Connection, title: str, body: str, *, keep: bool = True) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'files', ?, ?, '2026-07-14T09:15:00-07:00', NULL, ?, ?, '{}', ?, ?)",
        (USER_ID, f"doc-{title}", now_iso(), title, body, f"h-{title}",
         "keep" if keep else "drop"),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


#: A deterministic stand-in for an embedding model: three axes the tests can reason about,
#: so a ranking assertion is about the code rather than about a model's opinions.
def fake_embed(vocabulary: dict[str, list[float]], default: list[float] | None = None):  # type: ignore[no-untyped-def]
    def _embed(settings: Settings, texts: list[str]) -> list[list[float]]:
        del settings
        out = []
        for text in texts:
            lowered = text.lower()
            for word, vector in vocabulary.items():
                if word in lowered:
                    out.append(vector)
                    break
            else:
                out.append(default or [0.0, 0.0, 1.0])
        return out

    return _embed


VOCAB = {
    "housing": [1.0, 0.0, 0.0],
    "scholarship": [0.0, 1.0, 0.0],
    "immunization": [0.0, 0.0, 1.0],
}


class TestItFindsADocumentByWhatItWasAbout:
    def test_the_closest_document_ranks_first(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The question docs/02's five WHERE clauses cannot answer: which letter was this."""
        monkeypatch.setattr(search, "embed", fake_embed(VOCAB))
        a_document(conn, "Willow Hall contract", "housing agreement and deposit")
        a_document(conn, "AES award", "scholarship acceptance form")

        search.index(conn, sett)
        hits = search.search(conn, sett, "where do I live next year — housing")

        assert hits[0].title == "Willow Hall contract"
        assert hits[0].score > hits[1].score

    def test_only_kept_documents_are_indexed(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """Triage already ruled on these. Indexing a dropped item would put it back in
        front of the owner through a side door."""
        monkeypatch.setattr(search, "embed", fake_embed(VOCAB))
        a_document(conn, "kept", "housing agreement")
        a_document(conn, "dropped", "housing newsletter", keep=False)

        assert search.index(conn, sett) == 1
        assert [h.title for h in search.search(conn, sett, "housing")] == ["kept"]

    def test_an_empty_index_returns_nothing_rather_than_guessing(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """No LIKE fallback. A substring match dressed as a search is worse than an honest
        empty result, because it is indistinguishable from one that worked."""
        monkeypatch.setattr(search, "embed", fake_embed(VOCAB))
        a_document(conn, "Willow Hall contract", "housing agreement")

        assert search.search(conn, sett, "housing") == []


class TestIndexingIsResumableAndHonest:
    def test_re_running_indexes_only_what_is_new(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(search, "embed", fake_embed(VOCAB))
        a_document(conn, "one", "housing")
        assert search.index(conn, sett) == 1
        assert search.index(conn, sett) == 0

        a_document(conn, "two", "scholarship")
        assert search.index(conn, sett) == 1

    def test_coverage_states_what_was_not_searched(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """A search over a tenth of the ledger looks exactly like a search over all of it,
        right up until the answer is the part that was missing."""
        monkeypatch.setattr(search, "embed", fake_embed(VOCAB))
        for i in range(3):
            a_document(conn, f"doc{i}", "housing")

        search.index(conn, sett, limit=1)
        stats = search.coverage(conn, sett)

        assert stats == {
            "model": "stub-embed",
            "indexed": 1,
            "indexable": 3,
            "pending": 2,
        }

    def test_changing_the_embedding_model_re_indexes_rather_than_mixing_spaces(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """Two models' vectors are not comparable, and ranking them against each other
        returns plausible nonsense instead of an error. The model is part of the row's
        identity so that is unreachable rather than merely discouraged."""
        monkeypatch.setattr(search, "embed", fake_embed(VOCAB))
        a_document(conn, "one", "housing")
        search.index(conn, sett)

        other = sett.model_copy(update={"embedding_model": "different-embed"})
        assert search.coverage(conn, other)["indexed"] == 0
        assert search.index(conn, other) == 1

    def test_a_failing_endpoint_raises_rather_than_returning_a_worse_answer(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        def boom(settings: Settings, texts: list[str]) -> list[list[float]]:
            raise search.SearchError("connection refused")

        monkeypatch.setattr(search, "embed", boom)
        a_document(conn, "one", "housing")

        with pytest.raises(search.SearchError):
            search.index(conn, sett)


class TestItStaysAdditive:
    def test_nothing_that_decides_or_reports_reads_a_similarity_score(self) -> None:
        """The whole concession. docs/02: delete the index and every existing surface must
        still be correct — otherwise the ledger has quietly become the search-over-a-pile
        the product rejects. Asserted structurally, because a reviewer will not notice the
        first import that crosses this line."""
        from pathlib import Path

        from backglass.config import REPO_ROOT

        forbidden = [
            REPO_ROOT / "backglass" / "brief",
            REPO_ROOT / "backglass" / "plan",
            REPO_ROOT / "backglass" / "goals",
            REPO_ROOT / "backglass" / "web" / "panels.py",
        ]
        offenders: list[str] = []
        for target in forbidden:
            files = [target] if target.is_file() else sorted(Path(target).rglob("*.py"))
            for path in files:
                text = path.read_text()
                if "backglass.search" in text or "from backglass import search" in text:
                    offenders.append(str(path.relative_to(REPO_ROOT)))
        assert offenders == [], f"retrieval leaked into a load-bearing surface: {offenders}"
