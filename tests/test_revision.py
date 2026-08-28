"""Standing facts the record has moved past. `backglass/extract/revision.py`.

The pass exists because of one afternoon. `fact` 59 said "Joined Tray Favors team December
2025", active, extracted from the email in which the owner asked to *leave* Tray Favors —
the poison gate did everything right and the fact was true when written. `fact` 86 said the
placement reply was "PREPARED … NOT YET SENT", written six minutes before it was sent. Both
stayed active for four days, and `facts.owner_context` carried both into every model call
the product made.

What these hold is the shape of the answer, not the model's judgement. The verdicts are
hand-written JSON, as everywhere else in this suite: the model is not under test, the
guards around it are — the citation rule, the id intersection, the recurrence key, and the
one that matters most, that nothing here can ever write a fact active.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from backglass import facts
from backglass.config import Settings
from backglass.db import now_iso
from backglass.extract import revision
from backglass.extract.prompts import Prompt
from backglass.ledger import USER_ID


def _prompt() -> Prompt:
    from backglass.extract import prompts

    return prompts.load("revise-facts")


def _item(
    conn: sqlite3.Connection,
    *,
    external: str,
    title: str,
    body: str,
    author: str = "Someone",
    occurred_at: str = "2026-08-24T18:24:59+00:00",
    verdict: str = "keep",
) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'apple-mail', ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
        (USER_ID, external, now_iso(), occurred_at, author, title, body,
         f"h-{external}", verdict),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _fact(conn: sqlite3.Connection, settings: Settings, subject: str, key: str, value: str) -> int:
    return facts.remember(conn, settings, subject, key, value, note="seed", source="manual")


class FakeClient:
    """Returns canned verdicts. The model is not under test; the guards are."""

    def __init__(self, payloads: list[dict[str, Any]]):
        self.payloads = list(payloads)
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, user: str, **_: Any) -> Any:
        self.calls.append((system, user))
        payload = self.payloads.pop(0) if self.payloads else {"verdicts": []}
        return type("R", (), {"data": payload, "cost_usd": 0.0})()


# ── the Tray Favors case, end to end ─────────────────────────────────────────

TRAY_BODY = (
    "Hi Aimee, following up on my request to move off Tray Favors. I am no longer "
    "available Wednesday 4-8 PM now that BIO 181 lab runs to 4:45."
)


@pytest.fixture
def tray(conn, settings):  # type: ignore[no-untyped-def]
    """The ledger as it stood on 2026-08-27: a stale fact and the mail that overtook it."""
    fact_id = _fact(
        conn, settings, "work", "team-joined", "Joined Tray Favors team December 2025"
    )
    item_id = _item(
        conn,
        external="tray-1",
        title="Re: Request to change placement",
        body=TRAY_BODY,
        author="Dharsan Kesavan",
    )
    return fact_id, item_id


def test_a_fact_the_record_overtook_is_proposed_not_written(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """The whole design in one assertion. A wrong fact rewrite is a bad premise under
    every model call that follows, so this pass may never write anything active — the
    revision waits on the Memory page for a click, like every other candidate."""
    fact_id, item_id = tray
    client = FakeClient([{"verdicts": [{
        "fact_id": fact_id, "verdict": "overtaken", "confidence": 0.95,
        "cites_item": item_id, "quote": "my request to move off Tray Favors",
        "replacement": "Joined Tray Favors team December 2025; left it in August 2026.",
        "reason": "the owner asked to be moved off it",
    }]}])

    report = revision.run(conn, settings, client, prompt=_prompt())
    assert (report.judged, report.proposed, report.current) == (1, 1, 0)

    original = conn.execute("SELECT status FROM fact WHERE id = ?", (fact_id,)).fetchone()
    assert original["status"] == "active", "the pass may not supersede anything itself"

    waiting = facts.proposed(conn)
    assert [(p.subject, p.key) for p in waiting] == [("work", "team-joined")]
    assert "left it in August 2026" in waiting[0].value
    # And the proposal can be read back to the sentence that caused it.
    assert f"item {item_id}" in (waiting[0].note or "")
    assert "move off Tray Favors" in (waiting[0].note or "")


def test_accepting_the_revision_is_the_only_way_it_becomes_true(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """One door for "a fact becomes current", and the owner's click is it: accepting runs
    the same supersession path a manual `memory set` does."""
    fact_id, item_id = tray
    client = FakeClient([{"verdicts": [{
        "fact_id": fact_id, "verdict": "overtaken", "confidence": 0.95,
        "cites_item": item_id, "quote": "no longer available Wednesday 4-8 PM",
        "replacement": "Joined Tray Favors December 2025; off it since August 2026.",
    }]}])
    revision.run(conn, settings, client, prompt=_prompt())

    proposal = facts.proposed(conn)[0]
    facts.accept(conn, settings, proposal.fact_id)

    rows = {
        int(r["id"]): str(r["status"])
        for r in conn.execute("SELECT id, status FROM fact WHERE subject = 'work'")
    }
    assert rows[fact_id] == "superseded"
    live = [f for f in facts.recall(conn) if f.subject == "work" and f.key == "team-joined"]
    assert live and "off it since August 2026" in live[0].value


def test_the_owner_context_stops_carrying_the_stale_line(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Why this is worse than an inert row: the fact rides into every model call, so it is
    not one wrong line on one page — it is a wrong premise under triage, extraction and
    the planner until somebody notices."""
    fact_id, item_id = tray
    assert "Tray Favors team December 2025" in facts.owner_context(conn)

    client = FakeClient([{"verdicts": [{
        "fact_id": fact_id, "verdict": "overtaken", "confidence": 0.95,
        "cites_item": item_id, "quote": "move off Tray Favors",
        "replacement": "No longer on the Tray Favors team; joined December 2025, left August 2026.",
    }]}])
    revision.run(conn, settings, client, prompt=_prompt())
    # Proposed is invisible to owner_context — that is what makes proposing safe.
    assert "left August 2026" not in facts.owner_context(conn)

    facts.accept(conn, settings, facts.proposed(conn)[0].fact_id)
    assert "No longer on the Tray Favors team" in facts.owner_context(conn)


# ── the guards ───────────────────────────────────────────────────────────────

def test_a_revision_with_no_citation_is_discarded(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Silence is not evidence and neither is an assertion. Without the item and the
    words, this pass is an opinion about an old row."""
    fact_id, _item_id = tray
    client = FakeClient([{"verdicts": [{
        "fact_id": fact_id, "verdict": "overtaken", "confidence": 0.99,
        "cites_item": None, "quote": None, "replacement": "not on the team",
    }]}])
    report = revision.run(conn, settings, client, prompt=_prompt())
    assert (report.proposed, report.discarded) == (0, 1)
    assert facts.proposed(conn) == []


def test_a_quote_that_is_not_in_the_item_is_discarded(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """The guard against a fluent paraphrase standing in for having read the thing."""
    fact_id, item_id = tray
    client = FakeClient([{"verdicts": [{
        "fact_id": fact_id, "verdict": "overtaken", "confidence": 0.99,
        "cites_item": item_id, "quote": "I have resigned from the hospital entirely",
        "replacement": "no longer volunteers at Banner",
    }]}])
    report = revision.run(conn, settings, client, prompt=_prompt())
    assert (report.proposed, report.discarded) == (0, 1)
    assert facts.proposed(conn) == []


def test_a_revision_that_cannot_say_what_is_true_now_is_discarded(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """A replacement is required. "No longer true" is a deletion wearing a verdict's
    clothes, and the proposed row has to stand alone in the ledger."""
    fact_id, item_id = tray
    client = FakeClient([{"verdicts": [{
        "fact_id": fact_id, "verdict": "overtaken", "confidence": 0.99,
        "cites_item": item_id, "quote": "move off Tray Favors", "replacement": "  ",
    }]}])
    report = revision.run(conn, settings, client, prompt=_prompt())
    assert (report.proposed, report.discarded) == (0, 1)


def test_ids_the_pass_never_sent_are_discarded(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """The 2026-08-12 guard: `fact` and `source_item` ids overlap in range, so an id the
    model invented would otherwise land a revision on an unrelated row."""
    _fact_id, item_id = tray
    client = FakeClient([{"verdicts": [{
        "fact_id": 999999, "verdict": "overtaken", "confidence": 0.99,
        "cites_item": item_id, "quote": "move off Tray Favors", "replacement": "x",
    }]}])
    report = revision.run(conn, settings, client, prompt=_prompt())
    assert (report.judged, report.discarded) == (0, 1)


def test_an_unrelated_item_id_is_discarded(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    fact_id, _item_id = tray
    other = _item(conn, external="other", title="Lunch", body="see you at one")
    client = FakeClient([{"verdicts": [{
        "fact_id": fact_id, "verdict": "overtaken", "confidence": 0.99,
        "cites_item": other + 5000, "quote": "see you at one", "replacement": "x",
    }]}])
    report = revision.run(conn, settings, client, prompt=_prompt())
    assert (report.proposed, report.discarded) == (0, 1)


def test_a_current_fact_is_recorded_and_left_alone(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Most facts are permanent. A verdict of `current` costs one row so the same evidence
    is never paid for twice."""
    fact_id, _item_id = tray
    client = FakeClient([{"verdicts": [{
        "fact_id": fact_id, "verdict": "current", "confidence": 0.9,
    }]}])
    report = revision.run(conn, settings, client, prompt=_prompt())
    assert (report.judged, report.current, report.proposed) == (1, 1, 0)
    assert facts.proposed(conn) == []
    row = conn.execute("SELECT verdict, status FROM fact_check").fetchone()
    assert (row["verdict"], row["status"]) == ("current", "current")


# ── the recurrence key ───────────────────────────────────────────────────────

def test_a_judged_fact_is_not_re_judged_without_new_evidence(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Judged once, so two syncs over an unchanged ledger make one pass's calls and then
    none. A watermark on the run would re-pay for the same answers every sync."""
    fact_id, _item_id = tray
    client = FakeClient([
        {"verdicts": [{"fact_id": fact_id, "verdict": "current", "confidence": 0.9}]},
    ])
    assert revision.run(conn, settings, client, prompt=_prompt()).judged == 1
    again = FakeClient([])
    assert revision.run(conn, settings, again, prompt=_prompt()).judged == 0
    assert again.calls == [], "an unchanged ledger must not cost a call"


def test_new_evidence_re_opens_the_question(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Judged once is not judged forever — which is the whole failure being fixed. A fact
    judged current in August must be re-judged when September says otherwise."""
    fact_id, _item_id = tray
    first = FakeClient([
        {"verdicts": [{"fact_id": fact_id, "verdict": "current", "confidence": 0.9}]},
    ])
    revision.run(conn, settings, first, prompt=_prompt())

    later = _item(
        conn,
        external="tray-2",
        title="RE: Request to change placement",
        body="You are all set — your Wednesday Tray Favors shift has been released.",
        author="Munoz, Aimee C 1",
        occurred_at="2026-08-26T17:00:00+00:00",
    )
    second = FakeClient([{"verdicts": [{
        "fact_id": fact_id, "verdict": "overtaken", "confidence": 0.95,
        "cites_item": later, "quote": "your Wednesday Tray Favors shift has been released",
        "replacement": "Joined Tray Favors December 2025; the shift was released in August 2026.",
    }]}])
    report = revision.run(conn, settings, second, prompt=_prompt())
    assert (report.judged, report.proposed) == (1, 1)


def test_the_same_revision_is_not_proposed_twice(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Asking twice is nagging — the same guard `facts.apply_extracted` applies."""
    fact_id, item_id = tray
    verdict = {
        "fact_id": fact_id, "verdict": "overtaken", "confidence": 0.95,
        "cites_item": item_id, "quote": "move off Tray Favors",
        "replacement": "Off the Tray Favors team since August 2026.",
    }
    revision.run(conn, settings, FakeClient([{"verdicts": [verdict]}]), prompt=_prompt())
    later = _item(conn, external="tray-3", title="ping", body="anything new?")
    verdict_again = dict(verdict, cites_item=later, quote="anything new?")
    report = revision.run(
        conn, settings, FakeClient([{"verdicts": [verdict_again]}]), prompt=_prompt()
    )
    assert report.proposed == 0
    assert len(facts.proposed(conn)) == 1


def test_dropped_items_are_not_evidence(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """A dropped item is one triage judged not to bear on the owner's life. Re-reading it
    here would re-litigate that with a more expensive model."""
    _item(conn, external="junk", title="Sale", body="50% off everything", verdict="drop")
    assert revision.evidence(conn) == []


def test_a_ledger_with_nothing_read_makes_no_call(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Nothing has been read, so nothing can have overtaken anything. Guessing from the
    facts alone is not this pass's business."""
    _fact(conn, settings, "work", "team-joined", "Joined Tray Favors team December 2025")
    client = FakeClient([])
    assert revision.run(conn, settings, client, prompt=_prompt()).judged == 0
    assert client.calls == []


def test_dry_run_writes_nothing_and_still_says_what_it_would_do(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """A pass costs the same to read as to run, so counts alone make --dry-run pointless."""
    fact_id, item_id = tray
    client = FakeClient([{"verdicts": [{
        "fact_id": fact_id, "verdict": "overtaken", "confidence": 0.95,
        "cites_item": item_id, "quote": "move off Tray Favors",
        "replacement": "Off the Tray Favors team since August 2026.",
    }]}])
    report = revision.run(conn, settings, client, prompt=_prompt(), dry_run=True)
    assert report.proposed == 1
    assert facts.proposed(conn) == []
    assert conn.execute("SELECT COUNT(*) AS n FROM fact_check").fetchone()["n"] == 0
    assert any("Off the Tray Favors team" in line for line in report.verdicts)


def test_a_fact_superseded_elsewhere_mid_call_is_left_alone(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """The same re-read `relevance.apply` does: another surface may have moved the row
    while the call was out, and the verdict was formed against what it used to say."""
    fact_id, item_id = tray
    work = revision.Work(
        facts=revision.candidates(conn, through_item=0),
        items=revision.evidence(conn),
    )
    _fact(conn, settings, "work", "team-joined", "Owner updated this by hand")
    judgement = revision.Judgement(
        fact_id=fact_id, verdict="overtaken", confidence=0.95, cites_item=item_id,
        quote="move off Tray Favors", replacement="something else", reason=None,
    )
    report = revision.apply(conn, settings, work, [judgement])
    assert (report.judged, report.proposed) == (0, 0)


def test_the_owners_own_items_are_marked(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Load-bearing, not decorative. The first live run over the real Banner thread
    returned `current` for a fact that said a reply was "drafted, NOT YET SENT" while two
    of the items in front of it were that reply, sent. The author column held a bare
    address, so rule 7 needed three inferential steps to reach something the ledger knows
    for certain."""
    _fact_id, _item_id = tray
    mine = _item(
        conn, external="mine", title="Re: placement",
        body="Sending my top three now.", author="Dharsan Kesavan <alex.rivera@example.com>",
    )
    work = revision.Work(
        facts=revision.candidates(conn, through_item=0), items=revision.evidence(conn)
    )
    _static, user = revision.render(
        work, prompt=_prompt(), owner_emails=tuple(settings.owner_emails)
    )
    assert f"[item {mine}] " in user
    assert "YOU (the user wrote this)" in user
    # And a third party is not marked as the owner.
    assert user.count("YOU (the user wrote this)") == 1


def test_the_marker_is_off_when_no_addresses_are_configured(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """A blank `owner_emails` must not mark every item as the owner's — a substring match
    against the empty string is true of everything."""
    work = revision.Work(
        facts=revision.candidates(conn, through_item=0), items=revision.evidence(conn)
    )
    _static, user = revision.render(work, prompt=_prompt(), owner_emails=("", "   "))
    assert "YOU (the user wrote this)" not in user


# ── what counts as evidence ──────────────────────────────────────────────────
#
# Found by running the pass on the real ledger rather than by reading it. The window was
# ordered by `occurred_at`, which for a `canvas:ics` row is its DUE DATE — so "the newest
# things the ledger has read" was in fact "the coursework due furthest in the future",
# topped by an excused-absence form due in March 2027. Every fact was judged against a
# list of future deadlines, and the one revision it proposed cited "Self & Team Evaluation
# is due 2026-12-06" as grounds for rewriting the semester's course load.

def test_evidence_is_ordered_by_what_was_read_not_by_what_it_is_about(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """Insertion order is what "newest read" means, and it is what `through_item`
    watermarks — so the window and the recurrence key have to agree."""
    old_news = _item(
        conn, external="read-second", title="Aimee replied",
        body="Your placement change is in progress.", occurred_at="2026-08-20T10:00:00+00:00",
    )
    future = _item(
        conn, external="read-first", title="EC PS #1 due", body="due 2026-12-07",
        occurred_at="2026-12-07T06:59:59+00:00",
    )
    window = revision.evidence(conn, today="2026-08-27")
    ids = [int(i["id"]) for i in window]
    assert future not in ids, "a deadline in December is not something the ledger has read"
    assert ids[0] == old_news, "the newest row read comes first"


def test_nothing_dated_in_the_future_is_evidence(conn, settings) -> None:  # type: ignore[no-untyped-def]
    """A thing that has not happened cannot have overtaken a fact. Filtered on the date
    rather than the source: a calendar event next month is exactly as inert as a Canvas
    deadline is."""
    _item(conn, external="later", title="Exam 4", body="Exam 4",
          occurred_at="2026-12-08T00:00:00+00:00")
    _item(conn, external="calendar-later", title="Lab", body="Lab session",
          occurred_at="2026-09-30T00:00:00+00:00")
    today_row = _item(conn, external="today", title="Note", body="Something happened",
                      occurred_at="2026-08-27T09:00:00+00:00")
    assert [int(i["id"]) for i in revision.evidence(conn, today="2026-08-27")] == [today_row]


def test_the_watermark_is_the_window_it_was_formed_from(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """`through_item` is `max(id)` of the items sent, so a window ordered any other way
    would record a watermark that does not describe what was actually judged."""
    _fact_id, item_id = tray
    work = revision.Work(facts=[], items=revision.evidence(conn, today="2026-08-27"))
    assert work.through_item == item_id


# ── a cheap judge must not close a question a better one has not seen ────────

def test_again_re_opens_what_a_cheaper_model_already_closed(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """`sync` runs this pass on whatever backend is configured, and on the free tier it
    answers `current` to almost everything. Those verdicts cache under the same
    `(fact_id, through_item)` key, so a later hand-run on a stronger model finds nothing
    left to judge and silently agrees with the weaker one."""
    fact_id, item_id = tray
    weak = FakeClient([
        {"verdicts": [{"fact_id": fact_id, "verdict": "current", "confidence": 0.5}]},
    ])
    revision.run(conn, settings, weak, prompt=_prompt())

    blocked = FakeClient([])
    assert revision.run(conn, settings, blocked, prompt=_prompt()).judged == 0
    assert blocked.calls == []

    strong = FakeClient([{"verdicts": [{
        "fact_id": fact_id, "verdict": "overtaken", "confidence": 0.95,
        "cites_item": item_id, "quote": "move off Tray Favors",
        "replacement": "Off the Tray Favors team since August 2026.",
    }]}])
    report = revision.run(conn, settings, strong, prompt=_prompt(), again=True)
    assert (report.judged, report.proposed) == (1, 1)
    assert [p.value for p in facts.proposed(conn)] == [
        "Off the Tray Favors team since August 2026."
    ]


def test_the_second_look_replaces_the_first_verdict(tray, conn, settings) -> None:  # type: ignore[no-untyped-def]
    """`DO NOTHING` on the conflict would make `--again` pay for the calls and then throw
    the answers away. The only way to reach a conflict is a deliberate second look."""
    fact_id, item_id = tray
    revision.run(conn, settings, FakeClient([
        {"verdicts": [{"fact_id": fact_id, "verdict": "current", "confidence": 0.5}]},
    ]), prompt=_prompt())
    revision.run(conn, settings, FakeClient([{"verdicts": [{
        "fact_id": fact_id, "verdict": "overtaken", "confidence": 0.95,
        "cites_item": item_id, "quote": "move off Tray Favors",
        "replacement": "Off the Tray Favors team since August 2026.",
    }]}]), prompt=_prompt(), again=True)

    rows = conn.execute("SELECT verdict, status, proposed_fact FROM fact_check").fetchall()
    assert len(rows) == 1, "one row per (fact, evidence), not one per look"
    assert (rows[0]["verdict"], rows[0]["status"]) == ("overtaken", "proposed")
    assert rows[0]["proposed_fact"] is not None
