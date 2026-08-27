"""Answering a thread, in the owner's voice, with every claim traceable to a row.

`people/reachout.py` writes to someone the ledger has nothing on: three templates, a note
in the owner's words, no model call, and the same output twice. It is the right shape for
what it does and this module does not touch it.

It cannot answer a thread. The content of a reply is determined by what the other person
actually wrote, and no set of templates covers arbitrary prose. So this is the other path,
and the differences are all consequences of that one fact:

  - **A model call, because there is no alternative.** Routed through `extract/client.py`
    like every other call in the pipeline, so the spend cap (rule 7), the fallback chain
    and `model_call` logging come free rather than being reimplemented here.
  - **The thread is the evidence.** Every message read is named in `ReplyDraft.evidence`
    with its `source_item` id, so any sentence in the draft can be traced back to the
    message that licensed it. Rule 1 applied to generated prose.
  - **`stance` is required**, exactly as `reachout` requires `note`. A model handed a
    thread and no instruction writes a form letter with better grammar. The owner says
    what the reply should do; the model decides only how to say it.
  - **`draft/sweep.py` runs on the output before anyone sees it.** The model is told to
    avoid the tells and is not trusted to have managed it.

Nothing here writes a row. Two drafts of the same thread produce zero writes, which is
rule 3 satisfied by construction rather than by a test. The draft becomes evidence when
the owner sends it and the reply arrives back through the mail connector on the ordinary
path — the same way `reachout` has always worked.

**What this module refuses to do is as load-bearing as what it does.** It does not send.
`docs/10-tech-stack.md` chose a transactional provider for the morning brief and rejected
the Gmail API, and neither of those is a licence to send mail as the owner. The draft ends
as text and a `mailto:` link, and a person presses the button.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Sequence
from urllib.parse import quote

from backglass.config import Settings
from backglass.draft import sweep
from backglass.extract import prompts
from backglass.ledger import USER_ID

#: The prompt on disk. Stamped into every draft's evidence so a draft written under v1 is
#: distinguishable from one written under v2 — the same discipline extraction uses.
PROMPT_ID = "draft-reply"

#: The model is asked for these four keys and nothing else.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "answered": {"type": "array", "items": {"type": "string"}},
        "unstated": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["subject", "body"],
}

#: How much of each message reaches the prompt. A thread of six long emails would other-
#: wise dominate a careful-tier call, and the tail of a long message is rarely what the
#: reply is answering. The most recent message is exempt: it is the one being answered.
BODY_CHARS = 2_000
RECENT_BODY_CHARS = 6_000

#: How many of the owner's own sends are shown as a voice sample. Two or three is enough
#: to fix sentence length and sign-off; more crowds out the thread itself.
VOICE_SAMPLES = 3
VOICE_CHARS = 1_200

#: Mail source names whose rows are actual email. Chat and calendar rows share the table
#: but are not threads anyone replies to by mail.
MAIL_SOURCES = ("apple-mail", "gmail", "imap")

_PREFIX = re.compile(r"^\s*(?:(?:re|fwd|fw|aw|sv)\s*:\s*)+", re.IGNORECASE)
_ADDRESS = re.compile(r"<([^>]+@[^>]+)>|([^\s<>,;]+@[^\s<>,;]+)")


class ReplyError(ValueError):
    pass


def normalise_subject(title: object) -> str:
    """A subject with every `Re:`/`Fwd:` prefix stripped, for matching a thread.

    Mail clients stack prefixes and localise them, so the same conversation arrives as
    "Re: X", "RE: Re: X" and "Fwd: RE: X". Matching on the bare subject is what makes
    those one thread. Case is preserved; comparison is done lowercased by the caller.
    """
    text = " ".join(str(title or "").split())
    while True:
        stripped = _PREFIX.sub("", text, count=1)
        if stripped == text:
            return text.strip()
        text = stripped


def address_of(author: object) -> str | None:
    """The bare email address inside a `Name <a@b.com>` header, or None."""
    match = _ADDRESS.search(str(author or ""))
    if not match:
        return None
    return (match.group(1) or match.group(2) or "").strip().lower() or None


def display_name(author: object) -> str:
    """The human part of a From header, falling back to the address."""
    text = str(author or "").strip()
    head = text.split("<")[0].strip().strip('"')
    return head or address_of(text) or "unknown sender"


@dataclass(frozen=True)
class Message:
    """One message in the thread, as the prompt will see it."""

    source_item_id: int
    author: str
    occurred_at: str
    body: str
    is_owner: bool

    def render(self, *, limit: int) -> str:
        body = self.body.strip()
        if len(body) > limit:
            body = body[:limit].rstrip() + "\n[truncated]"
        who = "THE USER" if self.is_owner else display_name(self.author)
        return f"--- from {who}, {self.occurred_at}\n{body}"


@dataclass(frozen=True)
class ReplyDraft:
    """A draft, and everything needed to check it.

    Shaped deliberately like `people.reachout.Draft` — same `mailto()`, same `as_text()`,
    same `evidence` contract — so the two drafting paths present identically on the page
    and in the terminal. The owner should not have to learn two shapes for one job.
    """

    source_item_id: int
    to_name: str
    to_email: str | None
    subject: str
    body: str
    evidence: tuple[str, ...]
    #: Questions from their message the model says it answered. Shown so the owner can
    #: check the count against the message rather than taking the model's word.
    answered: tuple[str, ...] = ()
    #: What the stance asked for that the thread could not support. This is the honest
    #: channel for a gap: better here than invented in the body.
    unstated: tuple[str, ...] = ()
    #: Tells that survived `clean` and need a person. Advisory, never blocking.
    findings: tuple[sweep.Finding, ...] = ()
    cost_usd: float = 0.0

    def mailto(self) -> str | None:
        if not self.to_email:
            return None
        return (
            f"mailto:{quote(self.to_email)}"
            f"?subject={quote(self.subject)}&body={quote(self.body)}"
        )

    def as_text(self) -> str:
        to = self.to_email or self.to_name
        return f"To: {to}\nSubject: {self.subject}\n\n{self.body}"

    def sendable(self) -> bool:
        """False only when an unfilled placeholder survived. Taste never blocks."""
        return sweep.is_sendable(self.body)


@dataclass
class Thread:
    """The conversation, oldest first, plus what the ledger noticed about it."""

    subject: str
    messages: list[Message] = field(default_factory=list)
    #: True when the owner sent several messages within a few minutes of each other —
    #: it happened three times in one thread on 2026-08-24, and it means the draft must
    #: not assert what the recipient read.
    duplicate_sends: bool = False

    @property
    def latest_inbound(self) -> Message | None:
        for message in reversed(self.messages):
            if not message.is_owner:
                return message
        return None


def _owner_addresses(conn: sqlite3.Connection, settings: Settings) -> set[str]:
    """Every address that means "the owner", from settings and the fact table.

    Both sources, because either alone has been wrong: settings carries what was
    configured at install, and `identity/emails` carries what the owner has since told
    the app. A missed address makes the owner's own sends read as the other party's,
    which would put words in the recipient's mouth.
    """
    found = {str(a).strip().lower() for a in (settings.owner_emails or []) if a}
    for row in conn.execute(
        "SELECT value FROM fact WHERE user_id = ? AND status = 'active'"
        " AND subject = 'identity' AND key = 'emails'",
        (USER_ID,),
    ):
        for part in str(row["value"]).replace("·", ",").split(","):
            address = address_of(part) or part.split("(")[0].strip().lower()
            if address and "@" in address:
                found.add(address)
    return {a for a in found if a}


def thread_for(
    conn: sqlite3.Connection, settings: Settings, source_item_id: int
) -> Thread:
    """Every message in the same conversation, oldest first.

    Matched on the prefix-stripped subject rather than a threading header, because
    `external_id` is the provider's message id and the ledger keeps no parent pointer.
    Subject matching is what the owner would do by eye, and it is checkable: every id it
    gathered is named in the draft's evidence.
    """
    row = conn.execute(
        "SELECT id, source, author, title, occurred_at, body_text FROM source_item"
        " WHERE user_id = ? AND id = ?",
        (USER_ID, source_item_id),
    ).fetchone()
    if row is None:
        raise ReplyError(f"no source item #{source_item_id}")
    if str(row["source"]) not in MAIL_SOURCES:
        raise ReplyError(
            f"source item #{source_item_id} is from {row['source']!r}, which is not mail."
            " A reply draft needs an email thread; for a person the ledger has little on,"
            ' use `backglass reachout "<name>"`.'
        )

    bare = normalise_subject(row["title"])
    owners = _owner_addresses(conn, settings)
    rows = conn.execute(
        "SELECT id, author, title, occurred_at, body_text FROM source_item"
        " WHERE user_id = ? AND source = ? ORDER BY occurred_at",
        (USER_ID, str(row["source"])),
    ).fetchall()

    thread = Thread(subject=bare)
    for candidate in rows:
        if normalise_subject(candidate["title"]).lower() != bare.lower():
            continue
        address = address_of(candidate["author"])
        thread.messages.append(
            Message(
                source_item_id=int(candidate["id"]),
                author=str(candidate["author"] or ""),
                occurred_at=str(candidate["occurred_at"] or "")[:19],
                body=str(candidate["body_text"] or ""),
                is_owner=bool(address and address in owners),
            )
        )

    if not thread.messages:
        raise ReplyError(f"no messages found for {bare!r}")

    # Order by real instant, not by string. This ledger stores `occurred_at` with the
    # sender's own UTC offset, so the Todd thread holds "…T19:50:36-05:00" next to
    # "…T20:01:04-07:00" — two hours apart in the text and three hours apart in fact.
    # A thread handed to the model in the wrong order is a thread it will answer wrongly.
    thread.messages.sort(key=lambda m: (_instant(m.occurred_at) or "", m.source_item_id))
    thread.duplicate_sends = _sent_in_a_burst(thread.messages)
    return thread


def _instant(occurred_at: str) -> datetime | None:
    """The timestamp as a real moment, offset included, or None if unparseable."""
    try:
        parsed = datetime.fromisoformat(str(occurred_at))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


#: Two of the owner's own messages this close together, with nothing from anyone else
#: between them, are one message sent twice. Measured against the case that produced this
#: rule: three variants of the same email at 19:50, 20:01 and 20:04 on 2026-08-24.
BURST_MINUTES = 30


def _sent_in_a_burst(messages: Sequence[Message]) -> bool:
    """Did the owner send several versions of the same message back to back?

    Consecutive matters as much as close: two owner messages half an hour apart with the
    recipient's reply between them is an ordinary conversation, and the same two with
    nothing between them is a redraft. Only the second case means the draft must not
    claim which version was read.
    """
    previous: datetime | None = None
    for message in messages:
        if not message.is_owner:
            previous = None
            continue
        current = _instant(message.occurred_at)
        if previous is not None and current is not None:
            if abs((current - previous).total_seconds()) <= BURST_MINUTES * 60:
                return True
        previous = current
    return False


def voice_samples(
    conn: sqlite3.Connection, thread: Thread, *, limit: int = VOICE_SAMPLES
) -> list[Message]:
    """Emails the owner actually wrote, most recent first.

    Their own sends in this thread come first: same recipient, same register. Only when
    the thread has none does this fall back to their recent sends elsewhere, because a
    voice sample from a different relationship is better than none but worse than one
    from this one.
    """
    own = [m for m in reversed(thread.messages) if m.is_owner]
    if own:
        return own[:limit]

    ids = {m.source_item_id for m in thread.messages}
    authors = {m.author for m in thread.messages if m.is_owner}
    if not authors:
        return []
    placeholders = ",".join("?" for _ in authors)
    rows = conn.execute(
        f"SELECT id, author, occurred_at, body_text FROM source_item"
        f" WHERE user_id = ? AND author IN ({placeholders})"
        f" ORDER BY occurred_at DESC LIMIT ?",
        (USER_ID, *authors, limit + len(ids)),
    ).fetchall()
    return [
        Message(
            source_item_id=int(r["id"]),
            author=str(r["author"] or ""),
            occurred_at=str(r["occurred_at"] or "")[:19],
            body=str(r["body_text"] or ""),
            is_owner=True,
        )
        for r in rows
        if int(r["id"]) not in ids
    ][:limit]


def render(
    thread: Thread,
    *,
    stance: str,
    owner_context: str,
    voice: Sequence[Message],
    prompt: prompts.Prompt,
) -> str:
    """The user half of the call. Instructions live in the prompt file, not here."""
    latest = thread.latest_inbound
    rendered: list[str] = []
    for message in thread.messages:
        limit = RECENT_BODY_CHARS if latest and message is latest else BODY_CHARS
        rendered.append(message.render(limit=limit))

    if thread.duplicate_sends:
        rendered.append(
            "--- note: the user sent more than one version of their message in this"
            " thread. Do not state which one the recipient read."
        )

    samples = "\n\n".join(m.render(limit=VOICE_CHARS) for m in voice) or (
        "(no sample available - write plainly and do not invent a house style)"
    )
    return prompt.render_dynamic(
        owner_context=owner_context or "(nothing recorded)",
        voice=samples,
        thread="\n\n".join(rendered),
        stance=stance,
    )


def _evidence(
    thread: Thread,
    voice: Sequence[Message],
    prompt: prompts.Prompt,
    *,
    model: str,
    owner_context: str,
    fixed: Sequence[sweep.Finding],
) -> tuple[str, ...]:
    """One line per input, naming the row or file it came from. Rule 1."""
    lines = [
        f"thread: {len(thread.messages)} messages matched on subject"
        f" '{thread.subject}' — source_item ids "
        + ", ".join(f"#{m.source_item_id}" for m in thread.messages),
        "stance: typed by the owner for this draft, not extracted",
        f"prompt: {prompt.stamp} — {prompt.path}",
        f"model: {model} via backglass/extract/client.py",
    ]
    if voice:
        lines.append(
            "voice: the owner's own sends — "
            + ", ".join(f"#{m.source_item_id}" for m in voice)
        )
    else:
        lines.append(
            "voice: none available — no sent mail from the owner on this thread or"
            " elsewhere, so the draft has no house style behind it"
        )
    lines.append(
        f"context: backglass/context.py assemble() — {len(owner_context)} chars"
        if owner_context
        else "context: empty — the ledger had nothing to say about the owner today"
    )
    if thread.duplicate_sends:
        lines.append(
            "caution: the owner sent several versions of their message in this thread;"
            " the draft was told not to claim which one was read"
        )
    for finding in fixed:
        lines.append(f"sweep: {finding.line()}")
    lines.append(
        "attachments: not read — source_item.body_text never carries attachment text,"
        " so nothing in this draft describes one"
    )
    return tuple(lines)


def draft_reply(
    conn: sqlite3.Connection,
    settings: Settings,
    source_item_id: int,
    *,
    stance: str,
    client: Any,
    day: date | None = None,
    prompt: prompts.Prompt | None = None,
) -> ReplyDraft:
    """Draft one reply. Reads only; nothing here writes a row.

    `stance` is required for the same reason `reachout.draft` requires `note`: without a
    statement of what the reply should accomplish, a model produces something fluent that
    commits the owner to nothing and says nothing. The thread supplies the facts, the
    stance supplies the intent, and the model supplies only the sentences.
    """
    stance = (stance or "").strip()
    if not stance:
        raise ReplyError(
            "a reply needs --say: what you want this reply to do, in your words"
            " ('yes, and I'll call John this week' / 'no, but stay in touch')."
            " Without it the model writes a polite email that decides nothing."
        )

    from backglass import context

    spec = prompt or prompts.load(PROMPT_ID)
    thread = thread_for(conn, settings, source_item_id)
    latest = thread.latest_inbound
    if latest is None:
        raise ReplyError(
            f"every message on '{thread.subject}' is from the owner — there is nothing"
            " here to reply to. To nudge it, draft a follow-up instead."
        )

    voice = voice_samples(conn, thread)
    owner_context = context.assemble(conn, settings, day=day)
    user = render(
        thread,
        stance=stance,
        owner_context=owner_context,
        voice=voice,
        prompt=spec,
    )
    static, _ = spec.split()

    result = client.complete(
        system=static,
        user=user,
        schema=SCHEMA,
        model=settings.model_extract,
        budget_usd=settings.per_call_budget_usd,
    )
    payload = result.data if isinstance(result.data, dict) else {}
    body_raw = str(payload.get("body") or "").strip()
    if not body_raw:
        raise ReplyError(
            "the model returned no body. The thread and your stance are unchanged, so"
            " running this again is safe."
        )

    body, findings = sweep.report(body_raw)
    subject_raw = str(payload.get("subject") or "").strip()
    subject = sweep.clean(subject_raw) or f"Re: {thread.subject}"

    # What `clean` silently repaired is worth naming: the owner should know the model
    # produced an em dash even though they will never see one.
    fixed = tuple(f for f in sweep.findings(body_raw) if f.category == "watermark")

    return ReplyDraft(
        source_item_id=source_item_id,
        to_name=display_name(latest.author),
        to_email=address_of(latest.author),
        subject=subject,
        body=body,
        evidence=_evidence(
            thread,
            voice,
            spec,
            model=settings.model_extract,
            owner_context=owner_context,
            fixed=fixed,
        ),
        answered=tuple(str(x) for x in (payload.get("answered") or [])),
        unstated=tuple(str(x) for x in (payload.get("unstated") or [])),
        findings=findings,
        cost_usd=float(getattr(result, "cost_usd", 0.0) or 0.0),
    )


def to_json(record: ReplyDraft) -> str:
    return json.dumps(
        {
            "source_item_id": record.source_item_id,
            "to_name": record.to_name,
            "to_email": record.to_email,
            "subject": record.subject,
            "body": record.body,
            "answered": list(record.answered),
            "unstated": list(record.unstated),
            "findings": [f.line() for f in record.findings],
            "sendable": record.sendable(),
            "evidence": list(record.evidence),
            "cost_usd": round(record.cost_usd, 6),
        },
        indent=2,
    )


__all__ = [
    "Message",
    "PROMPT_ID",
    "ReplyDraft",
    "ReplyError",
    "Thread",
    "address_of",
    "display_name",
    "draft_reply",
    "normalise_subject",
    "thread_for",
    "to_json",
    "voice_samples",
]
