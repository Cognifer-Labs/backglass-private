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
from dataclasses import dataclass
from typing import Any

from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID

#: How much of a document is embedded. Whole documents, not chunks: the unit the owner
#: wants back is a document — "which letter said that" — and chunking would return a
#: fragment plus the job of finding which file it came from. A chunker is the right answer
#: when the corpus is a book; here it is the wrong unit dressed as sophistication.
MAX_CHARS = 8000


class SearchError(RuntimeError):
    """Indexing or querying could not run. Never raised into a brief or a plan."""


@dataclass(frozen=True)
class Hit:
    source_item_id: int
    title: str
    source: str
    occurred_at: str
    score: float


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


def embed(settings: Settings, texts: list[str]) -> list[list[float]]:
    """Embeddings from whatever `MODEL_BASE_URL` points at.

    The OpenAI `/v1/embeddings` shape, which Ollama, vLLM, DeepInfra, Together and the
    rest all implement — the same reason `openai_compatible` covers them for completions.
    Deliberately not routed through `extract.client`: that module's contract is a forced
    tool call returning a schema-checked object, and an embedding is neither.
    """
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


def index(conn: sqlite3.Connection, settings: Settings, *, limit: int = 200) -> int:
    """Embed kept source items that have no current vector. Returns how many were added.

    Bounded per call, and resumable by construction: the unique index is the watermark, so
    interrupting this is free and re-running it continues rather than restarting. A
    document whose text has changed re-embeds because `text_hash` is part of what is
    compared, and an immutable `source_item` means that is rare rather than the norm.
    """
    rows = conn.execute(
        "SELECT s.id, s.title, s.body_text FROM source_item s"
        " LEFT JOIN embedding e"
        "   ON e.source_item_id = s.id AND e.model = :model AND e.user_id = :user_id"
        " WHERE s.user_id = :user_id AND s.triage_verdict = 'keep'"
        "   AND s.body_text IS NOT NULL AND s.body_text <> ''"
        "   AND e.id IS NULL"
        " ORDER BY s.id LIMIT :limit",
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
            " (user_id, source_item_id, model, dim, vector, text_hash, chars, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                USER_ID,
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


def search(
    conn: sqlite3.Connection, settings: Settings, query: str, *, limit: int = 10
) -> list[Hit]:
    """The documents most like `query`, best first.

    Returns documents rather than passages, and returns nothing at all when the index is
    empty — an unindexed corpus is a fact to report, not a reason to fall back to LIKE and
    call the result a search.
    """
    if not query.strip():
        return []
    rows = conn.execute(
        "SELECT e.source_item_id, e.vector, s.title, s.source, s.occurred_at"
        " FROM embedding e JOIN source_item s ON s.id = e.source_item_id"
        " WHERE e.user_id = ? AND e.model = ?",
        (USER_ID, settings.embedding_model),
    ).fetchall()
    if not rows:
        return []

    wanted = _unpack(_pack(embed(settings, [query])[0]))
    scored = [
        Hit(
            source_item_id=int(row["source_item_id"]),
            title=str(row["title"] or "(no subject)"),
            source=str(row["source"]),
            occurred_at=str(row["occurred_at"]),
            score=_cosine(wanted, _unpack(row["vector"])),
        )
        for row in rows
    ]
    scored.sort(key=lambda hit: -hit.score)
    return scored[:limit]


def coverage(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """How much of the corpus is reachable, for `state` and the CLI.

    A search over a tenth of the ledger looks identical to a search over all of it, right
    up until the answer is missing — so the proportion is reported wherever the feature is,
    rather than left to be inferred from results that came back looking fine.
    """
    indexed = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM embedding WHERE user_id = ? AND model = ?",
            (USER_ID, settings.embedding_model),
        ).fetchone()["n"]
    )
    indexable = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM source_item WHERE user_id = ?"
            " AND triage_verdict = 'keep' AND body_text IS NOT NULL AND body_text <> ''",
            (USER_ID,),
        ).fetchone()["n"]
    )
    return {
        "model": settings.embedding_model,
        "indexed": indexed,
        "indexable": indexable,
        "pending": max(indexable - indexed, 0),
    }
