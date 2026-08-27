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
from typing import Any

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
            "kind": "source_item",
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
        first import that crosses this line.

        `dedup` is the one sanctioned reader and is deliberately not listed: it uses the
        vectors to *order* a queue of questions, never to decide anything, and returns an
        empty set when there is no index — asserted in
        `test_duplicate_pairs_is_empty_without_an_index_rather_than_raising` and again in
        `test_the_queue_is_unchanged_without_an_index` below. If a second soft reader ever
        appears, it belongs here in words before it belongs in code."""
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


class TestTheSameDocumentIsOneResult:
    """A fifth of the owner's corpus is a repeat of something else in it.

    254 of 1,247 documents, in 102 clusters — 39 copies of one scholarship advert, 8 of
    one acceptance reminder. Ranked individually they arrive together, because identical
    text scores identically, so a four-result page was four copies of one mail and the
    answer sat at position five. That is not a ranking problem to tune; it is the same
    document four times.
    """

    def _corpus(self, conn: sqlite3.Connection) -> None:
        for i in range(4):
            a_document(conn, f"Reminder to Accept your Award {i}", "scholarship acceptance")
        a_document(conn, "Willow Hall contract", "housing agreement and deposit")

    def test_copies_fold_into_one_hit_that_says_how_many(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(search, "embed", fake_embed(VOCAB))
        self._corpus(conn)
        search.index(conn, sett)

        hits = search.search(conn, sett, "scholarship")

        scholarship = [h for h in hits if h.copies > 1]
        assert len(scholarship) == 1
        assert scholarship[0].copies == 4
        assert len(scholarship[0].duplicate_ids) == 3

    def test_the_distinct_document_still_gets_its_own_row(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """Collapsing must reduce repeats, never variety — the whole point is that the
        answer at position five gets onto the page."""
        monkeypatch.setattr(search, "embed", fake_embed(VOCAB))
        self._corpus(conn)
        search.index(conn, sett)

        titles = [h.title for h in search.search(conn, sett, "scholarship", limit=2)]
        assert "Willow Hall contract" in titles

    def test_the_best_match_is_always_the_one_kept(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """Greedy from the top is what makes folding safe: collapsing can never promote a
        worse match over a better one, because the survivor is chosen before its copies."""
        monkeypatch.setattr(search, "embed", fake_embed(VOCAB))
        self._corpus(conn)
        search.index(conn, sett)

        folded = search.search(conn, sett, "scholarship")
        flat = search.search(conn, sett, "scholarship", collapse=False)
        assert folded[0].score == flat[0].score
        assert folded[0].source_item_id == flat[0].source_item_id

    def test_collapse_can_be_turned_off_for_a_caller_counting_the_corpus(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(search, "embed", fake_embed(VOCAB))
        self._corpus(conn)
        search.index(conn, sett)

        assert len(search.search(conn, sett, "scholarship", collapse=False)) == 5
        assert len(search.search(conn, sett, "scholarship")) == 2


class TestOneStoreServesBothQuestions:
    """0021 keyed vectors to source_item because documents were all that was retrieved.
    The same vectors answer "are these two promises the same promise", so the row names
    what it points at rather than a second table repeating six columns."""

    def test_commitments_and_documents_are_indexed_separately(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(search, "embed", fake_embed(VOCAB))
        a_document(conn, "Willow Hall contract", "housing agreement")
        conn.execute(
            "INSERT INTO commitment (user_id, direction, what, confidence, status,"
            " source_item_id, created_at) VALUES (1, 'i_owe', 'sign the housing contract',"
            " 0.9, 'open', 1, ?)",
            (now_iso(),),
        )

        assert search.index(conn, sett, kind="source_item") == 1
        assert search.index(conn, sett, kind="commitment") == 1
        assert search.coverage(conn, sett, "commitment")["indexed"] == 1
        assert search.coverage(conn, sett, "source_item")["indexed"] == 1

    def test_duplicate_pairs_is_empty_without_an_index_rather_than_raising(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The board renders whether or not anybody has run `search index`. Retrieval is
        additive (docs/02), so an unindexed installation must see the queue it always saw."""
        assert search.duplicate_pairs(conn, sett) == set()

    def test_an_unknown_kind_is_refused_rather_than_silently_indexing_nothing(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        with pytest.raises(search.SearchError):
            search.index(conn, sett, kind="nonsense")


class TestEmbeddingRequestsAreBoundedByCharacters:
    """A row count is the wrong unit and cost an hour to learn.

    250 iMessages is 16k characters; 250 mails is 828k. A batch size tuned on the first
    becomes a request eight times any sane limit on the second, and Ollama answers that
    with an intermittent 400 naming nothing — intermittent because it depends on what else
    the machine is holding, which is the worst shape of bug to meet in a month.
    """

    def test_a_large_batch_is_split_and_returned_in_order(
        self, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        sizes: list[int] = []

        def one_request(settings: Settings, texts: list[str]) -> list[list[float]]:
            sizes.append(sum(len(t) for t in texts))
            return [[float(len(t)), 0.0, 0.0] for t in texts]

        monkeypatch.setattr(search, "_embed_request", one_request)
        texts = ["x" * 40_000, "y" * 40_000, "z" * 40_000, "w" * 5]

        vectors = search.embed(sett, texts)

        assert len(sizes) > 1, "an oversized batch must be split"
        assert all(size <= search.EMBED_CHARS_PER_REQUEST for size in sizes)
        # Order is the contract: index() zips these back against its rows.
        assert [v[0] for v in vectors] == [40_000.0, 40_000.0, 40_000.0, 5.0]

    def test_a_small_batch_is_one_request(
        self, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        calls: list[int] = []
        monkeypatch.setattr(
            search,
            "_embed_request",
            lambda s, t: calls.append(len(t)) or [[1.0, 0.0, 0.0] for _ in t],
        )
        search.embed(sett, ["short", "also short"])
        assert calls == [2]

    def test_nothing_to_embed_makes_no_request(
        self, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(
            search, "_embed_request", lambda s, t: pytest.fail("should not be called")
        )
        assert search.embed(sett, []) == []

def test_embeddings_do_not_follow_chat_to_a_provider_that_has_none(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """One setting used to answer both "where is chat" and "where is /v1/embeddings".

    When chat moved to OpenRouter's free tier, that would have sent embedding calls there
    too — where `nomic-embed-text` does not exist. Every index run would die on a 404, and
    any vector that did come back would belong to a different model's space than the 1,248
    already stored, which the `embedding` identity forbids and which returns plausible
    nonsense rather than an error.
    """
    from tests.conftest import build_settings

    settings = build_settings(tmp_path).model_copy(
        update={
            "model_base_url": "https://openrouter.ai/api/v1",
            "embedding_base_url": "http://127.0.0.1:11434/v1",
        }
    )
    seen: dict[str, str] = {}

    def capture(request: Any, timeout: int = 0) -> Any:
        seen["url"] = request.full_url
        raise RuntimeError("stop here — the URL is the whole assertion")


    import backglass.search as search_mod

    monkeypatch.setattr(search_mod.urllib.request, "urlopen", capture)
    with pytest.raises(RuntimeError):
        search_mod.embed(settings, ["anything"])

    assert seen["url"].startswith("http://127.0.0.1:11434/v1")


def test_a_config_that_never_split_them_still_means_what_it_meant(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Empty `embedding_base_url` falls through to the chat URL — every existing config."""
    from tests.conftest import build_settings

    settings = build_settings(tmp_path).model_copy(
        update={"model_base_url": "http://127.0.0.1:11434/v1", "embedding_base_url": ""}
    )
    seen: dict[str, str] = {}

    def capture(request: Any, timeout: int = 0) -> Any:
        seen["url"] = request.full_url
        raise RuntimeError("stop — the URL is the whole assertion")

    import backglass.search as search_mod

    monkeypatch.setattr(search_mod.urllib.request, "urlopen", capture)
    with pytest.raises(RuntimeError):
        search_mod.embed(settings, ["anything"])

    assert seen["url"].startswith("http://127.0.0.1:11434/v1")
