"""Drafting a reply to a thread: what it reads, what it refuses, what it cites.

No test here calls a live API — the model is a fake returning a recorded payload, which
is the house rule and is also the only way to assert what the *prompt* was handed. Half
of these tests never look at the draft at all; they look at the `user` string the client
received, because everything this module is responsible for happens before the model runs.

The thread shapes are the owner's real ones from 2026-08-24/25, reduced: three variants of
one email sent fourteen minutes apart, mixed UTC offsets in `occurred_at`, and a reply
whose subject stacked prefixes.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from backglass.config import Settings
from backglass.draft import reply, sweep
from backglass.extract import prompts
from backglass.ledger import USER_ID

PAYLOAD = {
    "subject": "Re: Thank you for the introduction",
    "body": "Good afternoon Todd,\n\nYes, please do.\n\nBest,\nDharsan",
    "answered": ["whether to ask Mark McKenna"],
    "unstated": [],
}


class FakeResult:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.cost_usd = 0.02


class FakeClient:
    """Records what it was asked, so the tests can assert on the prompt."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data if data is not None else dict(PAYLOAD)
        self.calls: list[dict[str, Any]] = []

    def complete(self, **kwargs: Any) -> FakeResult:
        self.calls.append(kwargs)
        return FakeResult(self.data)

    @property
    def user(self) -> str:
        return str(self.calls[-1]["user"])


def _item(
    conn: sqlite3.Connection,
    *,
    author: str,
    title: str,
    occurred_at: str,
    body: str = "...",
    source: str = "apple-mail",
) -> int:
    cur = conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
        " occurred_at, author, title, body_text, content_hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            USER_ID,
            source,
            f"ext-{occurred_at}-{author}",
            "2026-08-25T00:00:00Z",
            occurred_at,
            author,
            title,
            body,
            f"hash-{occurred_at}-{author}",
        ),
    )
    return int(cur.lastrowid)


OWNER = "Dharsan Kesavan <alex.rivera@example.com>"
TODD = "Todd Altomare <todd.altomare@example.edu>"
SUBJECT = "Thank you for the introduction"


@pytest.fixture
def thread(conn: sqlite3.Connection) -> dict[str, int]:
    """The real shape: three owner variants minutes apart, then their reply."""
    ids = {
        "first": _item(
            conn, author=OWNER, title=SUBJECT,
            occurred_at="2026-08-24T19:50:36-05:00", body="Professor, good news.",
        ),
        "second": _item(
            conn, author=OWNER, title=SUBJECT,
            occurred_at="2026-08-24T20:01:04-07:00", body="Professor, some good news.",
        ),
        "third": _item(
            conn, author=OWNER, title=f"Re: {SUBJECT}",
            occurred_at="2026-08-24T20:04:02-07:00",
            body="One correction, and a question. How soon do I ask for terms?",
        ),
        "theirs": _item(
            conn, author=TODD, title=f"Re: {SUBJECT}",
            occurred_at="2026-08-25T13:28:01+00:00",
            body="Hey Dharsan, talk to John soon. I have a call with Mark. Let me know.",
        ),
    }
    conn.commit()
    return ids


@pytest.fixture
def prompt() -> prompts.Prompt:
    return prompts.load(reply.PROMPT_ID)


# --- subject and address parsing ------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["Re: X", "RE: X", "Fwd: RE: X", "Re: Re: Fwd:  X", "FW: X", "  Re:X  "],
)
def test_stacked_prefixes_resolve_to_one_subject(raw):
    assert reply.normalise_subject(raw) == "X"


def test_a_subject_that_merely_contains_re_is_untouched():
    assert reply.normalise_subject("Renewal reminder") == "Renewal reminder"


def test_address_and_name_come_out_of_a_from_header():
    assert reply.address_of(TODD) == "todd.altomare@example.edu"
    assert reply.display_name(TODD) == "Todd Altomare"


def test_a_bare_address_is_its_own_display_name():
    assert reply.address_of("a@b.com") == "a@b.com"
    assert reply.display_name("a@b.com") == "a@b.com"


# --- thread assembly ------------------------------------------------------------------


def test_the_thread_gathers_every_message_regardless_of_prefix(
    conn, settings, thread
):
    found = reply.thread_for(conn, settings, thread["theirs"])
    assert [m.source_item_id for m in found.messages] == [
        thread["first"], thread["second"], thread["third"], thread["theirs"]
    ]


def test_ordering_is_by_real_instant_not_by_string(conn, settings, thread):
    """`19:50-05:00` sorts before `20:01-07:00` as text and after it in fact.

    Handing the model a thread in the wrong order is handing it a different conversation.
    """
    found = reply.thread_for(conn, settings, thread["theirs"])
    instants = [reply._instant(m.occurred_at) for m in found.messages]
    assert instants == sorted(instants)


def test_the_owners_own_messages_are_recognised(conn, settings, thread):
    found = reply.thread_for(conn, settings, thread["theirs"])
    owned = {m.source_item_id for m in found.messages if m.is_owner}
    assert owned == {thread["first"], thread["second"], thread["third"]}


def test_the_latest_inbound_is_the_message_being_answered(conn, settings, thread):
    found = reply.thread_for(conn, settings, thread["theirs"])
    assert found.latest_inbound.source_item_id == thread["theirs"]


def test_three_variants_minutes_apart_are_flagged_as_a_burst(conn, settings, thread):
    assert reply.thread_for(conn, settings, thread["theirs"]).duplicate_sends is True


def test_an_ordinary_exchange_is_not_a_burst(conn, settings):
    _item(conn, author=OWNER, title="Ping", occurred_at="2026-08-01T09:00:00+00:00")
    _item(conn, author=TODD, title="Re: Ping", occurred_at="2026-08-01T09:10:00+00:00")
    last = _item(
        conn, author=OWNER, title="Re: Ping", occurred_at="2026-08-01T09:20:00+00:00"
    )
    conn.commit()
    assert reply.thread_for(conn, settings, last).duplicate_sends is False


def test_a_non_mail_row_is_refused_and_points_at_reachout(conn, settings):
    cal = _item(
        conn, author="", title="LSB 191", occurred_at="2026-08-25T14:30:00-07:00",
        source="calendar:asu",
    )
    conn.commit()
    with pytest.raises(reply.ReplyError, match="reachout"):
        reply.thread_for(conn, settings, cal)


def test_an_unknown_id_is_refused(conn, settings):
    with pytest.raises(reply.ReplyError, match="no source item"):
        reply.thread_for(conn, settings, 999_999)


# --- voice ----------------------------------------------------------------------------


def test_the_voice_sample_is_the_owners_own_sends_newest_first(conn, settings, thread):
    found = reply.thread_for(conn, settings, thread["theirs"])
    assert [m.source_item_id for m in reply.voice_samples(conn, found)] == [
        thread["third"], thread["second"], thread["first"]
    ]


# --- guards ---------------------------------------------------------------------------


def test_a_draft_without_a_stance_is_refused(conn, settings, thread, prompt):
    with pytest.raises(reply.ReplyError, match="--say"):
        reply.draft_reply(
            conn, settings, thread["theirs"], stance="  ",
            client=FakeClient(), prompt=prompt,
        )


def test_a_stance_free_refusal_costs_no_model_call(conn, settings, thread, prompt):
    client = FakeClient()
    with pytest.raises(reply.ReplyError):
        reply.draft_reply(
            conn, settings, thread["theirs"], stance="", client=client, prompt=prompt
        )
    assert client.calls == []


def test_a_thread_with_no_inbound_message_cannot_be_replied_to(conn, settings, prompt):
    mine = _item(
        conn, author=OWNER, title="Only me", occurred_at="2026-08-01T09:00:00+00:00"
    )
    conn.commit()
    with pytest.raises(reply.ReplyError, match="nothing"):
        reply.draft_reply(
            conn, settings, mine, stance="say yes", client=FakeClient(), prompt=prompt
        )


def test_an_empty_body_from_the_model_is_an_error_not_a_blank_draft(
    conn, settings, thread, prompt
):
    client = FakeClient({"subject": "Re: X", "body": "   "})
    with pytest.raises(reply.ReplyError, match="no body"):
        reply.draft_reply(
            conn, settings, thread["theirs"], stance="yes", client=client, prompt=prompt
        )


# --- what the prompt is handed --------------------------------------------------------


def test_the_stance_reaches_the_prompt_verbatim(conn, settings, thread, prompt):
    client = FakeClient()
    reply.draft_reply(
        conn, settings, thread["theirs"],
        stance="yes to Mark, and I will call John this week",
        client=client, prompt=prompt,
    )
    assert "yes to Mark, and I will call John this week" in client.user


def test_a_burst_thread_warns_the_model_not_to_guess_what_was_read(
    conn, settings, thread, prompt
):
    client = FakeClient()
    reply.draft_reply(
        conn, settings, thread["theirs"], stance="yes", client=client, prompt=prompt
    )
    assert "more than one version" in client.user


def test_the_instructions_are_sent_as_the_cacheable_system_half(
    conn, settings, thread, prompt
):
    """Every placeholder sits after every rule, so the prefix caches (prompts.py:80)."""
    client = FakeClient()
    reply.draft_reply(
        conn, settings, thread["theirs"], stance="yes", client=client, prompt=prompt
    )
    system = client.calls[-1]["system"]
    assert "WHAT THE DRAFT MUST NOT CONTAIN" in system
    assert "{{" not in system
    assert len(system) > len(client.user)


def test_the_careful_tier_is_used(conn, settings, thread, prompt):
    client = FakeClient()
    reply.draft_reply(
        conn, settings, thread["theirs"], stance="yes", client=client, prompt=prompt
    )
    assert client.calls[-1]["model"] == settings.model_extract


# --- the draft ------------------------------------------------------------------------


def test_the_draft_is_addressed_to_whoever_wrote_last(conn, settings, thread, prompt):
    record = reply.draft_reply(
        conn, settings, thread["theirs"], stance="yes",
        client=FakeClient(), prompt=prompt,
    )
    assert record.to_email == "todd.altomare@example.edu"
    assert record.to_name == "Todd Altomare"
    assert record.mailto().startswith("mailto:todd.altomare%40example.edu")


def test_watermarks_in_the_model_output_never_reach_the_owner(
    conn, settings, thread, prompt
):
    client = FakeClient(
        {"subject": "Re: X", "body": "Hi — I’d say “yes”… 2019–2024 was fine."}
    )
    record = reply.draft_reply(
        conn, settings, thread["theirs"], stance="yes", client=client, prompt=prompt
    )
    assert "—" not in record.body
    assert "’" not in record.body
    assert "2019-2024" in record.body
    assert not any(f.category == "watermark" for f in record.findings)


def test_a_watermark_that_was_repaired_is_still_reported_in_the_evidence(
    conn, settings, thread, prompt
):
    """The owner never sees the em dash, and should still know it was produced."""
    client = FakeClient({"subject": "Re: X", "body": "Hi — there."})
    record = reply.draft_reply(
        conn, settings, thread["theirs"], stance="yes", client=client, prompt=prompt
    )
    assert any("sweep:" in line for line in record.evidence)


def test_a_phrase_the_sweep_cannot_fix_is_surfaced_not_silently_kept(
    conn, settings, thread, prompt
):
    client = FakeClient(
        {"subject": "Re: X", "body": "I hope this email finds you well. Yes."}
    )
    record = reply.draft_reply(
        conn, settings, thread["theirs"], stance="yes", client=client, prompt=prompt
    )
    assert any(f.category == "phrase" for f in record.findings)
    assert record.sendable() is True


def test_a_leaked_placeholder_makes_the_draft_unsendable(
    conn, settings, thread, prompt
):
    client = FakeClient({"subject": "Re: X", "body": "Yes.\n\nBest,\n[Your Name]"})
    record = reply.draft_reply(
        conn, settings, thread["theirs"], stance="yes", client=client, prompt=prompt
    )
    assert record.sendable() is False


def test_a_missing_subject_falls_back_to_the_threads_own(
    conn, settings, thread, prompt
):
    client = FakeClient({"subject": "", "body": "Yes."})
    record = reply.draft_reply(
        conn, settings, thread["theirs"], stance="yes", client=client, prompt=prompt
    )
    assert record.subject == f"Re: {SUBJECT}"


# --- evidence (rule 1) ----------------------------------------------------------------


def test_every_message_read_is_named_in_the_evidence(conn, settings, thread, prompt):
    record = reply.draft_reply(
        conn, settings, thread["theirs"], stance="yes",
        client=FakeClient(), prompt=prompt,
    )
    joined = "\n".join(record.evidence)
    for key in ("first", "second", "third", "theirs"):
        assert f"#{thread[key]}" in joined


def test_the_prompt_stamp_is_recorded(conn, settings, thread, prompt):
    record = reply.draft_reply(
        conn, settings, thread["theirs"], stance="yes",
        client=FakeClient(), prompt=prompt,
    )
    assert any(prompt.stamp in line for line in record.evidence)


def test_the_evidence_says_attachments_were_not_read(conn, settings, thread, prompt):
    """`body_text` never holds attachment text, so the draft must not imply it did."""
    record = reply.draft_reply(
        conn, settings, thread["theirs"], stance="yes",
        client=FakeClient(), prompt=prompt,
    )
    assert any("attachments: not read" in line for line in record.evidence)


def test_the_burst_caution_reaches_the_evidence(conn, settings, thread, prompt):
    record = reply.draft_reply(
        conn, settings, thread["theirs"], stance="yes",
        client=FakeClient(), prompt=prompt,
    )
    assert any("several versions" in line for line in record.evidence)


# --- rule 3 ---------------------------------------------------------------------------


def test_drafting_twice_writes_nothing(conn, settings, thread, prompt):
    """A draft is a read. Two of them leave the ledger byte-identical."""
    before = conn.execute("SELECT count(*) AS n FROM source_item").fetchone()["n"]
    for _ in range(2):
        reply.draft_reply(
            conn, settings, thread["theirs"], stance="yes",
            client=FakeClient(), prompt=prompt,
        )
    after = conn.execute("SELECT count(*) AS n FROM source_item").fetchone()["n"]
    assert before == after
    assert conn.in_transaction is False


# --- the prompt file ------------------------------------------------------------------


def test_the_prompt_itself_carries_no_watermark_characters(prompt):
    """It quotes the phrases it bans, deliberately. It must not use the characters."""
    marks = [
        f for f in sweep.findings(prompt.text)
        if f.category in ("watermark", "emoji", "placeholder")
    ]
    assert not marks, "\n".join(f.line() for f in marks)


def test_json_round_trips(conn, settings, thread, prompt):
    import json

    record = reply.draft_reply(
        conn, settings, thread["theirs"], stance="yes",
        client=FakeClient(), prompt=prompt,
    )
    payload = json.loads(reply.to_json(record))
    assert payload["to_email"] == "todd.altomare@example.edu"
    assert payload["sendable"] is True
    assert payload["evidence"]
