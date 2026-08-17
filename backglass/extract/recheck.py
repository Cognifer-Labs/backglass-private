"""Chat commitments re-read against what the conversation said next.

Every other pass reads forward: a message arrives and may create or resolve an
obligation. That is how mail works, because completion gets announced — "here's that deck
I owed you". It is not how friends work. "Bring dress shoes" is answered by bringing dress
shoes, and the thread moves on.

Measured on the owner's ledger before this was written: 84 open commitments from
`imessage` across 16 conversations, **79 of them with later messages in the same chat**,
and not one ever closed by one. A forward-only signal cannot reach an obligation that
lives in chat, so the board fills with dead favours — "come over", "pick them up", "bring
bedsheet to wash" — which is exactly the noise that makes an owner stop trusting a board.

So this reads backwards: given a promise and the conversation that happened after it, is
the promise still live?

Four properties hold it together.

**Batched per conversation.** One call is given a chat's open commitments with their
ledger ids and the messages since that chat was last checked. Sixteen chats is the
ceiling; 84 per-commitment calls at the extract tier is not, and the same window would be
re-sent once per commitment.

**Ids in, ids back.** The model is handed the open list with ids, so its verdict is keyed
by id and no similarity matcher is needed. Two guards, both from the 2026-08-12 lesson
that a wrong-table id validates and writes to the wrong row: a returned id is intersected
with the set actually sent, and `status = 'open'` is re-read at write time.

**Silence is not evidence.** A closing verdict must cite a message id from the window and
quote it, and the quote is checked against that message's text before anything is applied.
A verdict that cannot is discarded, not stored. "Nobody mentioned it again" is exactly the
reasoning that would close every real obligation the owner has been quietly failing to do,
and it is the one failure here that nobody would ever see.

**Asymmetric by consequence.** Above `confidence_threshold` the commitment closes through
`actions.resolve`/`drop` with the citation recorded; below it the verdict waits as
`pending` on the review fragment for one click. A wrong open row is visible and
dismissible; a wrong close is silent data loss, and tasks/lessons.md has four entries
about paying for that.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from backglass.config import Settings
from backglass.db import now_iso
from backglass.extract.prompts import Prompt
from backglass.extract.schemas import RecheckResponse, json_schema
from backglass.ledger import USER_ID

SCHEMA = json_schema(RecheckResponse)

#: Messages per call. A conversation that has been ignored for a month should not send a
#: thousand lines to a careful-tier model; the newest are the ones that settle anything.
WINDOW_LIMIT = 120

#: How much of a message body reaches the prompt. Chat lines are short and the long ones
#: are pasted links.
BODY_LIMIT = 400


class RecheckError(ValueError):
    pass


@dataclass
class Message:
    item_id: int
    occurred_at: str
    author: str
    text: str


@dataclass
class ChatWork:
    """One conversation's worth of question: what is open, and what was said since."""

    chat_id: int
    key: str
    display: str
    commitments: list[dict[str, Any]] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    #: Highest source_item id in the window — the watermark this run would advance to.
    through: int = 0

    @property
    def sent_ids(self) -> set[int]:
        return {int(c["id"]) for c in self.commitments}

    @property
    def window_ids(self) -> set[int]:
        return {m.item_id for m in self.messages}


@dataclass
class Verdict:
    commitment_id: int
    verdict: str
    confidence: float
    cites: int | None
    quote: str | None
    reason: str | None


@dataclass
class Report:
    checked: list[str] = field(default_factory=list)
    applied: int = 0
    pending: int = 0
    discarded: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)


def candidates(
    conn: sqlite3.Connection, *, only: str | None = None
) -> list[ChatWork]:
    """Monitored chats that have open commitments *and* something new since last time.

    Both halves are required, and the watermark is what makes rule 3 hold: a chat with
    nothing new produces no call and therefore no writes, however often the sync runs.
    """
    chats = conn.execute(
        "SELECT id, key, display_name, rechecked_through FROM monitored_chat"
        " WHERE user_id = ? AND decision = 'monitor' ORDER BY id",
        (USER_ID,),
    ).fetchall()

    work: list[ChatWork] = []
    for chat in chats:
        key = str(chat["key"])
        display = str(chat["display_name"] or key)
        if only and only.lower() not in {key.lower(), display.lower()}:
            continue
        # `source_item.title` carries the chat a message belongs to — the display name for
        # a group, the handle for a one-to-one — and the monitored key is the lowercased
        # form of it. Compared case-insensitively rather than joined on an id, because
        # there is no id: the connector writes what the chat was called.
        opens = conn.execute(
            "SELECT c.id, c.what, c.due_at, c.direction, c.created_at, s.occurred_at"
            "  FROM commitment c JOIN source_item s ON s.id = c.source_item_id"
            " WHERE c.user_id = ? AND c.status = 'open' AND s.source = 'imessage'"
            "   AND LOWER(s.title) = LOWER(?) ORDER BY c.id",
            (USER_ID, key),
        ).fetchall()
        if not opens:
            continue

        floor = int(chat["rechecked_through"] or 0)
        # Only messages the pass has not already read. A commitment created from message
        # 900 is still re-read against 850 if the chat was never checked — the question is
        # what the conversation says, not what is newer than the promise.
        rows = conn.execute(
            "SELECT id, occurred_at, author, body_text FROM source_item"
            " WHERE user_id = ? AND source = 'imessage' AND LOWER(title) = LOWER(?)"
            "   AND id > ? ORDER BY id DESC LIMIT ?",
            (USER_ID, key, floor, WINDOW_LIMIT),
        ).fetchall()
        if not rows:
            continue

        messages = [
            Message(
                item_id=int(r["id"]),
                occurred_at=str(r["occurred_at"]),
                author=str(r["author"] or "?"),
                text=str(r["body_text"] or "")[:BODY_LIMIT],
            )
            for r in reversed(rows)
        ]
        work.append(
            ChatWork(
                chat_id=int(chat["id"]),
                key=key,
                display=display,
                commitments=[dict(r) for r in opens],
                messages=messages,
                through=max(m.item_id for m in messages),
            )
        )
    return work


def render(work: ChatWork, *, prompt: Prompt) -> tuple[str, str]:
    """The two prompt halves for one conversation."""
    commitments = "\n".join(
        f"[{c['id']}] {c['what']}"
        + (f" (due {str(c['due_at'])[:10]})" if c["due_at"] else "")
        + f" — promised {str(c['occurred_at'])[:10]}"
        for c in work.commitments
    )
    messages = "\n".join(
        f"(msg {m.item_id}) {str(m.occurred_at)[:16]} {m.author}: {m.text}"
        for m in work.messages
    )
    # `split()`'s static half is byte-identical on every call, so it goes as `system`
    # where a caching backend pays for it once. Both placeholders sit at the very end of
    # the prompt file for exactly that reason — `Prompt.split`'s docstring calls
    # placeholder position a cost decision, and this prompt was written to obey it.
    static, _ = prompt.split()
    return static, prompt.render_dynamic(commitments=commitments, messages=messages)


def parse(data: dict[str, Any], work: ChatWork) -> tuple[list[Verdict], int]:
    """Validated verdicts, and how many were thrown away. Every guard lives here.

    Discarded rather than corrected, in all four cases: a verdict this pass cannot fully
    trust is one it must not act on, and a "best effort" repair is how a wrong close would
    get written while looking careful.
    """
    parsed = RecheckResponse.model_validate(data)
    quotes = {m.item_id: m.text for m in work.messages}
    kept: list[Verdict] = []
    discarded = 0
    for v in parsed.verdicts:
        # 1. An id the pass never sent. Every id here is a bare int and the tables overlap
        #    in range, so this is the difference between closing the right row and closing
        #    a stranger (2026-08-12).
        if v.commitment_id not in work.sent_ids:
            discarded += 1
            continue
        if v.verdict == "open":
            kept.append(_verdict(v))
            continue
        # 2. A closing verdict with no citation. Silence is not evidence.
        if v.cites is None or not (v.quote or "").strip():
            discarded += 1
            continue
        # 3. A citation outside the window — including a message from another chat.
        if v.cites not in work.window_ids:
            discarded += 1
            continue
        # 4. A quote that is not in the message it names. This is the guard against a
        #    fluent-sounding paraphrase standing in for evidence: the citation has to be
        #    checkable, and checking it costs one substring test.
        if _normalize(v.quote or "") not in _normalize(quotes.get(v.cites, "")):
            discarded += 1
            continue
        kept.append(_verdict(v))
    return kept, discarded


def _verdict(v: Any) -> Verdict:
    return Verdict(
        commitment_id=int(v.commitment_id),
        verdict=str(v.verdict),
        confidence=float(v.confidence),
        cites=v.cites,
        quote=v.quote,
        reason=v.reason,
    )


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def apply(
    conn: sqlite3.Connection,
    settings: Settings,
    work: ChatWork,
    verdicts: list[Verdict],
    *,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Write what survived. Returns (applied, pending).

    `open` verdicts are not recorded at all: "still open" is what the row already says,
    and storing one per commitment per sync would be a table that grows forever to
    describe nothing.
    """
    applied = pending = 0
    for v in verdicts:
        if v.verdict == "open":
            continue
        # Re-read at write time. The pass may have taken a minute, and the owner may have
        # closed it by hand in that minute — the second half of the 2026-08-12 guard.
        row = conn.execute(
            "SELECT status FROM commitment WHERE id = ? AND user_id = ?",
            (v.commitment_id, USER_ID),
        ).fetchone()
        if row is None or str(row["status"]) != "open":
            continue

        confident = v.confidence >= settings.confidence_threshold
        if dry_run:
            applied += confident
            pending += not confident
            continue

        conn.execute(
            "INSERT INTO commitment_recheck (user_id, commitment_id, verdict, confidence,"
            " source_item_id, quote, reason, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, commitment_id, source_item_id) DO NOTHING",
            (
                USER_ID, v.commitment_id, v.verdict, v.confidence, v.cites,
                v.quote, v.reason, "applied" if confident else "pending", now_iso(),
            ),
        )
        if confident:
            from backglass.web import actions

            note = f'recheck: "{(v.quote or "").strip()}" (msg {v.cites})'
            if v.verdict == "done":
                actions.resolve(conn, v.commitment_id, note)
            else:
                actions.drop(conn, v.commitment_id, note)
            applied += 1
        else:
            pending += 1

    if not dry_run:
        conn.execute(
            "UPDATE monitored_chat SET rechecked_through = ? WHERE id = ? AND user_id = ?",
            (work.through, work.chat_id, USER_ID),
        )
    return applied, pending


def run(
    conn: sqlite3.Connection,
    settings: Settings,
    client: Any,
    *,
    prompt: Prompt,
    dry_run: bool = False,
    only: str | None = None,
) -> Report:
    """One pass over every conversation with something new to say.

    Rule 5's unit is the loop item: a chat whose call fails is recorded and skipped, and
    the other fifteen still run. A model outage must not stop the sync it hangs off.
    """
    report = Report()
    for work in candidates(conn, only=only):
        report.checked.append(work.display)
        try:
            _, user = render(work, prompt=prompt)
            result = client.complete(
                system="",
                user=user,
                schema=SCHEMA,
                model=settings.model_extract,
                budget_usd=settings.per_call_budget_usd,
            )
            verdicts, discarded = parse(result.data, work)
            report.cost_usd += float(getattr(result, "cost_usd", 0.0) or 0.0)
            report.discarded += discarded
            applied, pending = apply(conn, settings, work, verdicts, dry_run=dry_run)
            report.applied += applied
            report.pending += pending
        except Exception as exc:  # noqa: BLE001 — rule 5, per chat
            report.errors.append(f"{work.display}: {type(exc).__name__}: {exc}")
    return report
