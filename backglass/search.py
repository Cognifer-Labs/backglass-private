"""Finding a document by what it was about. Owner's ruling, 2026-08-10.

docs/02 §Why no vector database argued that the queries are fixed and known, which is
true of the brief, the schedule and the dashboard and is not true of the drop folder.
"Which letter mentioned the deposit deadline" is not a `WHERE` clause over a typed column
and does not become one, because no schema anticipates every question about a document
that has not arrived yet.

The concession is bounded, and the bounds are the reason it is safe:

- **Additive, never load-bearing.** Nothing in the brief, planner, dashboard or goals
  reads a similarity score. Drop this module and every existing surface is still correct.
  That is what stops the ledger becoming the search-over-a-pile the product rejects.
- **Retrieval finds documents; extraction still states facts.** A `fact` keeps coming
  from the extraction path with a source sentence behind it. This is how the owner
  reaches evidence, not how the system forms beliefs — rule 1 is untouched.
- **Local by default.** Embedding goes through the same `openai_compatible` backend as
  everything else, so a drop folder of contracts and scanned letters can be indexed with
  nothing leaving the machine (docs/08).

No sqlite-vec, faiss or numpy. A personal ledger holds thousands of documents, and a
cosine over a few thousand float32 vectors is milliseconds of pure Python. The moment
that stops being true is the moment to add an index, and the schema does not prevent it.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Any

from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID

#: How much of a document is embedded. Whole documents, not chunks: the unit the owner
#: wants back is a document — "which letter said that" — and chunking would return a
#: fragment plus the job of finding which file it came from. A chunker is the right answer
#: when the corpus is a book; here it is the wrong unit dressed as sophistication.
MAX_CHARS = 8000

#: Where two rows stop being two things. Measured rather than chosen: at 0.97 the owner's
#: corpus collapses 1,247 documents to 993, and the clusters it forms are the ones a
#: person would — 39 copies of one scholarship advert, 8 of one acceptance reminder.
#: Lower starts merging different mail from one sender; higher misses reworded siblings.
NEAR_DUPLICATE = 0.97

#: The bar for "these two promises are the same promise". Looser than NEAR_DUPLICATE
#: because a commitment is one restated sentence rather than a whole document, so there is
#: far less text for the difference to hide in. At 0.92 it finds 46 pairs against the
#: token-set matcher's 39, and the 13 it adds are all real.
SAME_COMMITMENT = 0.92


class SearchError(RuntimeError):
    """Indexing or querying could not run. Never raised into a brief or a plan."""


@dataclass(frozen=True)
class Hit:
    source_item_id: int
    title: str
    source: str
    occurred_at: str
    score: float
    #: How many near-identical documents this one stands for, itself included. One means
    #: one. Higher means the others were folded away and are reachable by their ids.
    copies: int = 1
    #: The oldest of them, when there is more than one. "Said 39 times between April and
    #: August" is the useful fact about a repeated advert; the newest alone is not.
    first_seen: str = ""
    duplicate_ids: tuple[int, ...] = ()


def _pack(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def _unpack(blob: bytes) -> array.array[float]:
    out = array.array("f")
    out.frombytes(blob)
    return out


def _cosine(a: array.array[float], b: array.array[float]) -> float:
    if len(a) != len(b):
        # Two models' vectors are not comparable, and ranking them against each other
        # produces plausible nonsense rather than an error. The identity in the schema
        # makes this unreachable; this is the assertion that keeps it unreachable.
        raise SearchError(f"vector dimensions differ ({len(a)} vs {len(b)})")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


#: How much text goes in one request. A row count is the wrong unit and cost an hour to
#: learn: 250 iMessages is 16k characters and 250 mails is 828k, so a batch size tuned on
#: the first silently becomes a request eight times any sane limit on the second. Ollama
#: answers that with a 400 that names nothing, and it is intermittent because it depends
#: on what else the machine is holding — the worst shape of bug to meet in a month's time.
EMBED_CHARS_PER_REQUEST = 100_000


def embed(settings: Settings, texts: list[str]) -> list[list[float]]:
    """Embeddings from whatever `MODEL_BASE_URL` points at, in requests it can hold.

    The OpenAI `/v1/embeddings` shape, which Ollama, vLLM, DeepInfra, Together and the
    rest all implement — the same reason `openai_compatible` covers them for completions.
    Deliberately not routed through `extract.client`: that module's contract is a forced
    tool call returning a schema-checked object, and an embedding is neither.

    Split by characters rather than by count, so a caller may hand over any mixture of
    one-line messages and eight-thousand-character letters without knowing this exists.
    Order is preserved; a single text over the budget still goes on its own, because
    `MAX_CHARS` already bounds it and refusing it here would drop a document silently.
    """
    out: list[list[float]] = []
    batch: list[str] = []
    size = 0
    for text in texts:
        if batch and size + len(text) > EMBED_CHARS_PER_REQUEST:
            out.extend(_embed_request(settings, batch))
            batch, size = [], 0
        batch.append(text)
        size += len(text)
    if batch:
        out.extend(_embed_request(settings, batch))
    return out


def _embed_request(settings: Settings, texts: list[str]) -> list[list[float]]:
    """One call to the endpoint. Split out so `embed` is the batching policy and this is
    the transport, and so a test can exercise either without the other."""
    if not texts:
        return []
    base = (settings.model_base_url or settings.deepinfra_base_url).rstrip("/")
    request = urllib.request.Request(
        f"{base}/embeddings",
        data=json.dumps({"model": settings.embedding_model, "input": texts}).encode(),
        headers={
            "Content-Type": "application/json",
            # Harmless against a local server that ignores it, required by a hosted one.
            "Authorization": f"Bearer {settings.model_api_key or 'not-needed'}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.embedding_timeout_seconds) as raw:
            payload = json.loads(raw.read())
    except urllib.error.HTTPError as exc:
        raise SearchError(f"embedding endpoint returned {exc.code} at {base}") from exc
    except Exception as exc:  # noqa: BLE001 — URLError, timeout, malformed JSON
        raise SearchError(f"embedding call failed: {type(exc).__name__}: {exc}") from exc

    try:
        return [item["embedding"] for item in payload["data"]]
    except (KeyError, TypeError) as exc:
        raise SearchError(f"embedding response had no data array: {str(payload)[:200]}") from exc


def _indexable_text(row: sqlite3.Row) -> str:
    title = str(row["title"] or "")
    body = str(row["body_text"] or "")
    return f"{title}\n\n{body}"[:MAX_CHARS].strip()


#: What each kind embeds, and what it is missing a vector for. Two queries rather than a
#: generic one, because "kept documents with body text" and "open promises I owe" are
#: different questions and a shared abstraction over them would say less than both.
_PENDING = {
    "source_item": (
        "SELECT s.id, s.title, s.body_text FROM source_item s"
        " LEFT JOIN embedding e ON e.kind = 'source_item' AND e.ref_id = s.id"
        "   AND e.model = :model AND e.user_id = :user_id"
        " WHERE s.user_id = :user_id AND s.triage_verdict = 'keep'"
        "   AND s.body_text IS NOT NULL AND s.body_text <> ''"
        "   AND e.id IS NULL ORDER BY s.id LIMIT :limit"
    ),
    "commitment": (
        "SELECT c.id, c.what AS title, '' AS body_text FROM commitment c"
        " LEFT JOIN embedding e ON e.kind = 'commitment' AND e.ref_id = c.id"
        "   AND e.model = :model AND e.user_id = :user_id"
        " WHERE c.user_id = :user_id AND c.status = 'open' AND e.id IS NULL"
        " ORDER BY c.id LIMIT :limit"
    ),
}


def index(
    conn: sqlite3.Connection, settings: Settings, *, limit: int = 200, kind: str = "source_item"
) -> int:
    """Embed rows of `kind` that have no current vector. Returns how many were added.

    Bounded per call, and resumable by construction: the unique index is the watermark, so
    interrupting this is free and re-running it continues rather than restarting.
    """
    if kind not in _PENDING:
        raise SearchError(f"no such embedding kind: {kind}")
    rows = conn.execute(
        _PENDING[kind],
        {"user_id": USER_ID, "model": settings.embedding_model, "limit": limit},
    ).fetchall()
    if not rows:
        return 0

    texts = [_indexable_text(row) for row in rows]
    vectors = embed(settings, texts)
    if len(vectors) != len(rows):
        raise SearchError(f"asked for {len(rows)} embeddings, got {len(vectors)}")

    added = 0
    for row, text, vector in zip(rows, texts, vectors, strict=True):
        conn.execute(
            "INSERT OR IGNORE INTO embedding"
            " (user_id, kind, ref_id, model, dim, vector, text_hash, chars, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                USER_ID,
                kind,
                int(row["id"]),
                settings.embedding_model,
                len(vector),
                _pack(vector),
                hashlib.sha256(text.encode()).hexdigest(),
                len(text),
                now_iso(),
            ),
        )
        added += 1
    return added


def duplicate_pairs(
    conn: sqlite3.Connection, settings: Settings, *, threshold: float = SAME_COMMITMENT
) -> set[tuple[int, int]]:
    """Open commitments that mean the same thing, by vector rather than by wording.

    The second signal for `dedup.suspects`, which until now compared token sets. Both are
    kept because they are complementary, not competing: over the owner's 191 open promises
    the token-set matcher finds 39 pairs, embeddings find 46, and each finds some the
    other misses. Wording catches "AES" against "AES"; meaning catches "you are back in
    Arizona" against "he's back in Arizona".

    Returns normalized (low, high) pairs. Empty when nothing is indexed — the caller then
    behaves exactly as it did before this existed, which is what keeps the feature
    additive rather than a new precondition for the board rendering.
    """
    rows = conn.execute(
        "SELECT e.ref_id, e.vector FROM embedding e"
        " JOIN commitment c ON c.id = e.ref_id"
        " WHERE e.user_id = ? AND e.kind = 'commitment' AND e.model = ?"
        "   AND c.status = 'open'",
        (USER_ID, settings.embedding_model),
    ).fetchall()
    vectors = [(int(row["ref_id"]), _unpack(row["vector"])) for row in rows]

    out: set[tuple[int, int]] = set()
    for i, (left_id, left) in enumerate(vectors):
        for right_id, right in vectors[i + 1 :]:
            if _cosine(left, right) >= threshold:
                out.add((min(left_id, right_id), max(left_id, right_id)))
    return out


def search(
    conn: sqlite3.Connection,
    settings: Settings,
    query: str,
    *,
    limit: int = 10,
    collapse: bool = True,
) -> list[Hit]:
    """The documents most like `query`, best first, one entry per distinct document.

    Returns documents rather than passages, and returns nothing at all when the index is
    empty — an unindexed corpus is a fact to report, not a reason to fall back to LIKE and
    call the result a search.

    `collapse=False` returns every copy separately, which is what a caller counting the
    corpus wants and never what a reader does.
    """
    if not query.strip():
        return []
    rows = conn.execute(
        "SELECT e.ref_id, e.vector, s.title, s.source, s.occurred_at"
        " FROM embedding e JOIN source_item s ON s.id = e.ref_id"
        " WHERE e.user_id = ? AND e.kind = 'source_item' AND e.model = ?",
        (USER_ID, settings.embedding_model),
    ).fetchall()
    if not rows:
        return []

    wanted = _unpack(_pack(embed(settings, [query])[0]))
    vectors = {int(row["ref_id"]): _unpack(row["vector"]) for row in rows}
    scored = [
        Hit(
            source_item_id=int(row["ref_id"]),
            title=str(row["title"] or "(no subject)"),
            source=str(row["source"]),
            occurred_at=str(row["occurred_at"]),
            score=_cosine(wanted, vectors[int(row["ref_id"])]),
        )
        for row in rows
    ]
    scored.sort(key=lambda hit: -hit.score)
    return _collapse(scored, vectors)[:limit] if collapse else scored[:limit]


def _collapse(hits: list[Hit], vectors: dict[int, array.array[float]]) -> list[Hit]:
    """Fold near-identical documents into their best-ranked representative.

    A fifth of the owner's corpus is a repeat of something else in it — 254 of 1,247
    documents, in 102 clusters, the largest being 39 copies of one scholarship advert and
    8 of one acceptance reminder. Ranked individually they arrive together, because
    identical text scores identically, so a four-result page can be four copies of one
    mail and the answer sits at position five. That is not a ranking problem to tune; it
    is the same document four times.

    Greedy from the top, which is what makes it safe: the highest-scoring member is always
    the one kept, so collapsing can never promote a worse match over a better one. The
    ones folded away keep their ids on the survivor, so nothing is hidden — "39 copies,
    first seen 14 April" is more than any one of them said alone.

    Read-time only. `source_item` is immutable and kept forever (docs/03), and the repeats
    are real evidence that the advert really did arrive 39 times.
    """
    kept: list[Hit] = []
    folded: set[int] = set()
    for hit in hits:
        if hit.source_item_id in folded:
            continue
        mine = vectors.get(hit.source_item_id)
        duplicates = [
            other
            for other in hits
            if other.source_item_id not in folded
            and other.source_item_id != hit.source_item_id
            and mine is not None
            and _cosine(mine, vectors[other.source_item_id]) >= NEAR_DUPLICATE
        ]
        folded.update(other.source_item_id for other in duplicates)
        dates = sorted([hit.occurred_at, *(o.occurred_at for o in duplicates)])
        kept.append(
            replace(
                hit,
                copies=1 + len(duplicates),
                first_seen=dates[0] if duplicates else "",
                duplicate_ids=tuple(o.source_item_id for o in duplicates),
            )
        )
    return kept


#: What "everything of this kind" means, per kind. Mirrors `_PENDING`'s WHERE clauses —
#: a coverage figure computed against a different population than the indexer walks is a
#: number that reads as reassurance and is not one.
_INDEXABLE = {
    "source_item": (
        "SELECT COUNT(*) AS n FROM source_item WHERE user_id = ?"
        " AND triage_verdict = 'keep' AND body_text IS NOT NULL AND body_text <> ''"
    ),
    "commitment": (
        "SELECT COUNT(*) AS n FROM commitment WHERE user_id = ? AND status = 'open'"
    ),
}


def coverage(
    conn: sqlite3.Connection, settings: Settings, kind: str = "source_item"
) -> dict[str, Any]:
    """How much of `kind` is reachable, for `state` and the CLI.

    A search over a tenth of the ledger looks identical to a search over all of it, right
    up until the answer is missing — so the proportion is reported wherever the feature is,
    rather than left to be inferred from results that came back looking fine.
    """
    if kind not in _INDEXABLE:
        raise SearchError(f"no such embedding kind: {kind}")
    indexed = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM embedding WHERE user_id = ? AND kind = ? AND model = ?",
            (USER_ID, kind, settings.embedding_model),
        ).fetchone()["n"]
    )
    indexable = int(conn.execute(_INDEXABLE[kind], (USER_ID,)).fetchone()["n"])
    return {
        "kind": kind,
        "model": settings.embedding_model,
        "indexed": indexed,
        "indexable": indexable,
        "pending": max(indexable - indexed, 0),
    }
