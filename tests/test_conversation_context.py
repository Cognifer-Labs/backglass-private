"""The few messages before the one being extracted.

Extraction reads one source_item, which is right for mail and wrong for chat. On the
owner's real ledger triage legitimately keeps "ye", "Sure", "5 rn" and "Coming*" — each
costs a full extraction call and none means anything alone. "Coming*" sits under "When
pickle today and where" and "Like 6:30 at pecos", and only together are they a plan.
"""

from __future__ import annotations

import json
import sqlite3

from backglass.extract import commitments as tier2


def message(
    conn: sqlite3.Connection,
    *,
    n: int,
    body: str,
    chat: str = "Pih ball",
    handle: str = "+1555",
    at: str = "2026-08-01T10:00:00+00:00",
    source: str = "imessage",
    is_from_me: bool = False,
) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, body_text, raw_json, content_hash)"
        " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, f"m{n}", at, at, handle, body,
         json.dumps({"chat": chat, "handle": handle, "is_from_me": is_from_me}), f"h{n}"),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class TestContext:
    def test_it_returns_the_conversation_above_the_message(
        self, conn: sqlite3.Connection
    ) -> None:
        message(conn, n=1, body="When pickle today and where", at="2026-08-01T10:00:00+00:00")
        message(conn, n=2, body="Like 6:30 at pecos", at="2026-08-01T10:01:00+00:00")
        focal = message(conn, n=3, body="Coming*", at="2026-08-01T10:02:00+00:00")

        context = tier2.conversation_context(conn, focal)

        assert "When pickle today and where" in context
        assert "Like 6:30 at pecos" in context
        assert "Coming*" not in context, "the focal message is not its own context"

    def test_it_reads_oldest_first(self, conn: sqlite3.Connection) -> None:
        """A conversation reads downwards. The query takes the nearest neighbours with a
        newest-first LIMIT, so the order has to be put back."""
        message(conn, n=1, body="first", at="2026-08-01T10:00:00+00:00")
        message(conn, n=2, body="second", at="2026-08-01T10:01:00+00:00")
        focal = message(conn, n=3, body="third", at="2026-08-01T10:02:00+00:00")

        context = tier2.conversation_context(conn, focal)

        assert context.index("first") < context.index("second")

    def test_another_conversation_never_leaks_in(self, conn: sqlite3.Connection) -> None:
        """Context from the wrong chat is worse than none: it would let the model resolve
        a reference against people who were never in the room."""
        message(conn, n=1, body="secret from another group", chat="SLT")
        focal = message(conn, n=2, body="Coming*", chat="Pih ball")

        assert "secret" not in tier2.conversation_context(conn, focal)

    def test_later_messages_are_not_context(self, conn: sqlite3.Connection) -> None:
        """Rule 4's shape again: a claim is resolved against what was already said, not
        against what the conversation went on to say."""
        focal = message(conn, n=1, body="Coming*", at="2026-08-01T10:00:00+00:00")
        message(conn, n=2, body="said afterwards", at="2026-08-01T11:00:00+00:00")

        assert "afterwards" not in tier2.conversation_context(conn, focal)

    def test_the_owners_own_lines_are_marked(self, conn: sqlite3.Connection) -> None:
        """Direction is the whole question a commitment answers. A transcript that does
        not say who spoke makes "I'll bring it" unattributable."""
        message(conn, n=1, body="can you bring the ball", is_from_me=False)
        focal = message(conn, n=2, body="yeah", is_from_me=True)

        assert "me:" not in tier2.conversation_context(conn, focal).split("\n")[0]
        assert tier2.conversation_context(conn, focal).startswith("+1555:")

    def test_it_is_bounded(self, conn: sqlite3.Connection) -> None:
        """Paid for on every extraction call, so it earns its place by being small."""
        for n in range(40):
            message(
                conn, n=n, body=f"line {n} " + "x" * 200,
                at=f"2026-08-01T10:{n:02d}:00+00:00",
            )
        focal = message(conn, n=99, body="Coming*", at="2026-08-01T11:00:00+00:00")

        context = tier2.conversation_context(conn, focal)

        assert len(context) <= tier2.CONTEXT_CHARS + 250
        assert len(context.split("\n")) <= tier2.CONTEXT_TURNS

    def test_an_item_with_no_conversation_gets_none(self, conn: sqlite3.Connection) -> None:
        """A note, a calendar event, a mail with no thread: those carry their own context
        in a subject and a body, and matching nothing is the right answer."""
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, body_text, raw_json, content_hash)"
            " VALUES (1, 'apple-notes', 'n1', 'now', 'now', 'a note', '{}', 'hn')"
        )
        note_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

        assert tier2.conversation_context(conn, note_id) == ""


class TestItReachesThePrompt:
    def test_the_rendered_prompt_carries_the_conversation(
        self, conn: sqlite3.Connection, settings
    ) -> None:  # type: ignore[no-untyped-def]
        from backglass.extract import prompts

        message(conn, n=1, body="Like 6:30 at pecos")
        focal = message(conn, n=2, body="Coming*")
        item = dict(conn.execute("SELECT * FROM source_item WHERE id = ?", (focal,)).fetchone())

        _system, rendered = tier2.render_parts(
            item,
            prompt=prompts.load("extract-commitments"),
            settings=settings,
            context=tier2.conversation_context(conn, focal),
        )

        assert "Like 6:30 at pecos" in rendered
        assert "context only" in rendered.lower()

    def test_an_item_with_no_context_still_renders(
        self, conn: sqlite3.Connection, settings
    ) -> None:  # type: ignore[no-untyped-def]
        """Every placeholder must be supplied or `Prompt.render` raises — a missing
        `{{context}}` would fail every extraction, not just the conversational ones."""
        from backglass.extract import prompts

        focal = message(conn, n=1, body="alone")
        item = dict(conn.execute("SELECT * FROM source_item WHERE id = ?", (focal,)).fetchone())

        _system, rendered = tier2.render_parts(
            item, prompt=prompts.load("extract-commitments"), settings=settings
        )

        assert "no earlier messages" in rendered
