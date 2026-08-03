"""docs/05, hard requirements B1 through B7.

The requirements table is the spec, so the tests are named after it. Each one states the
requirement it enforces, because a year from now the useful question about a failing test
here is "which promise did I break", not "what does this assert".
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backglass.brief import daily, deliver, render
from backglass.brief.model import (
    Brief,
    Line,
    MissingProvenance,
    Section,
    SourceRef,
)
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID, Ledger

TODAY = date(2026, 7, 30)
BASE = "http://127.0.0.1:8765"


@pytest.fixture(autouse=True)
def sync_is_alive(conn):  # type: ignore[no-untyped-def]
    """Every test here is about a brief's *content*, so each one assumes the pipeline
    ran. Said once, out loud: without a completed run the ledger has never synced and
    heartbeat.py puts that at the top of the brief, which is correct and is covered in
    tests/test_heartbeat.py rather than silently absorbed here."""
    from tests.conftest import healthy_run

    healthy_run(conn)


def a_source(n: int = 1) -> SourceRef:
    return SourceRef(
        source="gmail:personal",
        external_id=f"m{n}",
        occurred_at="2026-07-14T09:15:00-07:00",
        title="Migration plan",
    )


def seed(conn, settings: Settings, rows: list[dict]) -> None:  # type: ignore[no-untyped-def]
    """Insert commitments directly. The extraction path is covered in test_commitments."""
    ledger = Ledger(conn, settings)
    for index, row in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at, "
            " author, title, body_text, raw_json, content_hash, triage_verdict) "
            "VALUES (?, 'gmail:personal', ?, ?, ?, ?, ?, 'b', '{}', ?, 'keep')",
            (
                USER_ID,
                f"m{index}",
                now_iso(),
                row.get("occurred_at", "2026-07-14T09:15:00-07:00"),
                row.get("author", "Dana <dana@example.gov>"),
                row.get("title", "Migration plan"),
                f"hash{index}",
            ),
        )
        source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        entity_id = ledger.resolve_entity(row.get("counterparty", "Dana <dana@example.gov>"))
        conn.execute(
            "INSERT INTO commitment (user_id, direction, counterparty_entity_id, what, due_at, "
            " confidence, status, source_item_id, created_at, rollover_count) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
            (
                USER_ID,
                row["direction"],
                entity_id,
                row["what"],
                row.get("due_at"),
                row.get("confidence", 0.9),
                source_id,
                now_iso(),
                row.get("rollover_count", 0),
            ),
        )


# ── B2: every line links to its source ────────────────────────────────────


def test_b2_a_line_cannot_be_constructed_without_provenance() -> None:
    """ "A brief line with no provenance does not ship."

    Not a warning, not a filter — unconstructable. CLAUDE.md: "Trust collapses after two
    unsourced wrong claims and never comes back."
    """
    with pytest.raises(TypeError):
        Line("Migration plan due today")  # type: ignore[call-arg]
    with pytest.raises(MissingProvenance):
        Line("Migration plan due today", None)  # type: ignore[arg-type]


def test_b2_render_is_a_hard_failure_not_a_warning() -> None:
    """PROMPT.md §Session 4: "make that a hard failure in the renderer, not a warning"."""
    brief = Brief(generated_for_date=TODAY.isoformat())
    section = Section(priority=4, title="Slipping")
    section.lines.append(Line("ok", a_source()))
    brief.add(section)
    object.__setattr__(section.lines[0], "provenance", None)  # forced past the constructor

    with pytest.raises(MissingProvenance):
        render.to_html(brief, base_url=BASE)
    with pytest.raises(MissingProvenance):
        render.to_text(brief, base_url=BASE)


def test_b2_every_rendered_line_carries_a_working_link(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    seed(
        conn,
        settings,
        [
            {"direction": "i_owe", "what": "revised migration plan", "due_at": "2026-07-31"},
            {"direction": "owed_to_me", "what": "budget sheet", "due_at": "2026-08-05"},
        ],
    )
    brief = daily.build(conn, settings, TODAY)
    html = render.to_html(brief, base_url=BASE)

    assert brief.all_lines()
    for line in brief.all_lines():
        url = line.provenance.url(BASE)
        assert url.startswith("http")
        assert url in html


def test_gmail_provenance_deep_links_to_the_message() -> None:
    assert a_source().url(BASE) == "https://mail.google.com/mail/u/0/#all/m1"


# ── B3: empty sections are omitted ────────────────────────────────────────


def test_b3_empty_sections_are_never_rendered(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """ "A brief that is mostly empty headers trains you to stop opening it."

    At Phase 2 there is no planner and no goal engine, so five of the eight sections have
    no data. None of their headers may appear.
    """
    seed(conn, settings, [{"direction": "i_owe", "what": "plan", "due_at": "2026-07-31"}])
    brief = daily.build(conn, settings, TODAY)
    html = render.to_html(brief, base_url=BASE)

    assert [s.title for s in brief.ordered()] == ["Slipping"]
    for absent in ("Timezone change", "Today", "Capacity", "Goals", "Rolling over"):
        assert absent not in html


def test_a_completely_empty_ledger_still_says_something(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """Silence is the one output docs/05 B6 rules out."""
    brief = daily.build(conn, settings, TODAY)
    assert brief.sections == []
    assert brief.notes
    assert "Nothing open" in render.to_text(brief, base_url=BASE)


# ── B4: no item appears in two sections ───────────────────────────────────


def test_b4_precedence_is_the_docs05_section_order(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """A commitment that is both rolling over and slipping belongs to Slipping (§4),
    not Rolling over (§7)."""
    seed(
        conn,
        settings,
        [
            {
                "direction": "i_owe",
                "what": "revised plan",
                "due_at": "2026-07-31",
                "rollover_count": 4,
            },
        ],
    )
    brief = daily.build(conn, settings, TODAY)

    titles = [s.title for s in brief.ordered()]
    assert "Slipping" in titles
    assert "Rolling over" not in titles, "the lower-numbered section wins"
    assert len(brief.all_lines()) == 1


def test_b4_deduplicate_reports_what_it_removed() -> None:
    brief = Brief(generated_for_date=TODAY.isoformat())
    early = Section(priority=4, title="Slipping")
    late = Section(priority=8, title="Needs review")
    early.lines.append(Line("plan", a_source(), commitment_id=7))
    late.lines.append(Line("plan?", a_source(), commitment_id=7))
    brief.add(early)
    brief.add(late)

    assert brief.deduplicate() == 1
    assert [s.title for s in brief.sections] == ["Slipping"]


# ── B1: under 400 words ───────────────────────────────────────────────────


def test_b1_word_limit_is_enforced_and_the_truncation_is_stated(
    conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """ "Enforced in code; truncate the lowest-priority section and say so."

    Sixty commitments across two sections is far past 400 words. The lowest-priority
    section must go, and the brief must admit it rather than quietly shrinking.
    """
    rows = [
        {
            "direction": "owed_to_me" if index % 2 else "i_owe",
            "what": f"deliverable number {index} with a deliberately wordy description",
            "due_at": "2026-07-31",
            "counterparty": f"Person{index} <p{index}@example.com>",
        }
        for index in range(60)
    ]
    seed(conn, settings, rows)
    brief = daily.build(conn, settings, TODAY)

    assert brief.word_count() <= 400, brief.word_count()
    assert brief.notes, "a truncated brief must say what it dropped"
    assert any("omitted for length" in note.text for note in brief.notes)
    assert "omitted for length" in render.to_text(brief, base_url=BASE)


def test_b1_never_truncates_to_nothing() -> None:
    """A brief truncated to zero sections is the silence B6 exists to prevent."""
    brief = Brief(generated_for_date=TODAY.isoformat())
    section = Section(priority=4, title="Slipping")
    section.lines.extend(Line("word " * 200, a_source(n), commitment_id=n) for n in range(3))
    brief.add(section)

    brief.enforce_word_limit()
    assert len(brief.sections) == 1


# ── B5, B6, B7 ────────────────────────────────────────────────────────────


def test_b5_generation_touches_no_network(conn, settings: Settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """ "Generated from ledger state only. No live API calls at send time."

    A 06:00 brief that depends on Gmail being reachable is a brief that silently stops.
    """
    import urllib.request

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("brief generation attempted a network call")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    seed(conn, settings, [{"direction": "i_owe", "what": "plan", "due_at": "2026-07-31"}])
    brief = daily.build(conn, settings, TODAY)
    render.to_html(brief, base_url=BASE)
    render.to_text(brief, base_url=BASE)


def test_b6_a_generation_failure_produces_a_notice_not_silence() -> None:
    brief = daily.failure_brief(TODAY, "OperationalError")
    text = render.to_text(brief, base_url=BASE)
    assert "failed" in text
    assert "OperationalError" in text
    assert brief.generated_for_date == TODAY.isoformat()


def test_b6_the_failure_notice_carries_no_ledger_content() -> None:
    """docs/08: body_text never appears in an error report, and a failure notice is one."""
    brief = daily.failure_brief(TODAY, "sqlite3.OperationalError")
    text = render.to_text(brief, base_url=BASE)
    assert "body" not in text.lower()
    assert len(text) < 400


def test_b7_a_persisted_brief_gets_a_tracking_pixel(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """ "Record brief.opened_at via tracking pixel or link click, and use it."""
    seed(conn, settings, [{"direction": "i_owe", "what": "plan", "due_at": "2026-07-31"}])
    brief = daily.build(conn, settings, TODAY)
    brief_id = daily.persist(conn, brief, render.to_markdown(brief, base_url=BASE))

    html = render.to_html(brief, base_url=BASE, brief_id=brief_id)
    assert f"{BASE}/b/{brief_id}.gif" in html

    row = conn.execute("SELECT * FROM brief WHERE id = ?", (brief_id,)).fetchone()
    assert row["word_count"] == brief.word_count()
    assert row["opened_at"] is None


def test_regenerating_a_brief_does_not_erase_that_it_was_opened(
    conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """The owner read that morning's brief. Regenerating it does not make that untrue."""
    seed(conn, settings, [{"direction": "i_owe", "what": "plan", "due_at": "2026-07-31"}])
    brief = daily.build(conn, settings, TODAY)
    brief_id = daily.persist(conn, brief, "md")
    conn.execute("UPDATE brief SET opened_at = ? WHERE id = ?", (now_iso(), brief_id))

    again = daily.persist(conn, brief, "md v2")
    assert again == brief_id
    assert conn.execute("SELECT opened_at FROM brief WHERE id = ?", (brief_id,)).fetchone()[
        "opened_at"
    ]


# ── section content ───────────────────────────────────────────────────────


def test_overdue_items_appear_in_slipping(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """An item three days late is the most slipping thing in the ledger, and docs/05 has
    no overdue section for it to land in."""
    seed(conn, settings, [{"direction": "i_owe", "what": "late plan", "due_at": "2026-07-27"}])
    brief = daily.build(conn, settings, TODAY)
    line = brief.all_lines()[0]
    assert line.status == "overdue"
    assert "overdue 3d" in line.text


def test_low_confidence_never_enters_slipping_or_awaiting(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """CLAUDE.md rule 2: low-confidence extractions go to a review queue, never into the
    brief as fact. They appear as a question, which is a different speech act."""
    seed(
        conn,
        settings,
        [
            {
                "direction": "i_owe",
                "what": "maybe a plan",
                "due_at": "2026-07-31",
                "confidence": 0.4,
            },
        ],
    )
    brief = daily.build(conn, settings, TODAY)

    assert [s.title for s in brief.ordered()] == ["Needs review"]
    line = brief.all_lines()[0]
    assert line.status == "needs_review"
    assert line.text.endswith("confident)")
    assert "Did you owe" in line.text


def test_awaiting_reports_the_age_of_the_promise_not_the_row(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """A backfill would otherwise report a six-month-old promise as one day old."""
    seed(
        conn,
        settings,
        [
            {
                "direction": "owed_to_me",
                "what": "budget sheet",
                "occurred_at": "2026-07-14T09:15:00-07:00",
            },
        ],
    )
    brief = daily.build(conn, settings, TODAY)
    assert "16d" in brief.all_lines()[0].text


def test_a_failing_source_is_named_above_everything(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """docs/05: "Gmail auth expired 3 days ago, this brief is incomplete." Each of these
    is more valuable than any section it displaces."""
    from backglass.connectors import credentials

    seed(conn, settings, [{"direction": "i_owe", "what": "plan", "due_at": "2026-07-31"}])
    credentials.mark_failed(conn, "gmail:personal", "invalid_grant")
    brief = daily.build(conn, settings, TODAY)

    first = brief.ordered()[0]
    assert first.title == "Attention"
    assert "incomplete" in first.lines[0].text
    assert first.lines[0].status == "overdue"


def test_a_degraded_run_is_declared(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        "INSERT INTO run (user_id, started_at, degraded) VALUES (?, ?, 1)",
        (USER_ID, now_iso()),
    )
    seed(conn, settings, [{"direction": "i_owe", "what": "plan", "due_at": "2026-07-31"}])
    brief = daily.build(conn, settings, TODAY)
    assert any("Spend cap reached" in line.text for line in brief.all_lines())


# ── email constraints ─────────────────────────────────────────────────────


def test_email_html_obeys_the_docs05_constraints(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    seed(conn, settings, [{"direction": "i_owe", "what": "plan", "due_at": "2026-07-31"}])
    html = render.to_html(daily.build(conn, settings, TODAY), base_url=BASE)

    assert "<style" not in html, "no <style> block; inline every style"
    assert "var(--" not in html, "no CSS variables; email does not have them"
    assert "class=" not in html, "no external stylesheet to hook into"
    assert "display:flex" not in html and "display:grid" not in html
    assert html.count("<table") >= 2, "table-based layout"
    assert f"background:{render.PAPER}" in html, "cream set explicitly on the outer table"
    assert "box-shadow" not in html and "border-radius" not in html


def test_every_ink_fill_carries_a_black_keyline(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """design-system.md §8 rule 2. Gold is below 3:1 on paper and the keyline is the
    mitigation, so a fill without one is an accessibility regression, not a style nit."""
    seed(
        conn,
        settings,
        [
            {"direction": "i_owe", "what": "plan", "due_at": "2026-08-01"},
            {"direction": "owed_to_me", "what": "sheet"},
        ],
    )
    html = render.to_html(daily.build(conn, settings, TODAY), base_url=BASE)

    chips = html.split("<span")[1:]
    filled = [
        c
        for c in chips
        if any(
            f"background:{ink}" in c
            for ink in (render.GOLD, render.TURQUOISE, render.VERMILION, render.INK)
        )
    ]
    assert filled, "the fixture should produce at least one filled chip"
    for chip in filled:
        assert f"border:2px solid {render.INK}" in chip


def test_status_is_never_colour_alone(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """design-system.md §8 rule 3: icon plus label, always."""
    seed(conn, settings, [{"direction": "owed_to_me", "what": "sheet"}])
    html = render.to_html(daily.build(conn, settings, TODAY), base_url=BASE)
    assert "AWAITING" in html
    assert "○" in html


def test_needs_review_renders_dashed_with_no_fill(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """design-system.md §4: "in a system where color means certainty, a guess does not
    get an ink"."""
    seed(conn, settings, [{"direction": "i_owe", "what": "maybe", "confidence": 0.3}])
    html = render.to_html(daily.build(conn, settings, TODAY), base_url=BASE)
    chip = [f for f in html.split("<span")[1:] if "REVIEW" in f][0]
    assert "dashed" in chip
    assert "background:transparent" in chip


def test_plaintext_is_generated_from_data_not_by_stripping_tags(
    conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    seed(conn, settings, [{"direction": "i_owe", "what": "plan", "due_at": "2026-07-31"}])
    brief = daily.build(conn, settings, TODAY)
    text = render.to_text(brief, base_url=BASE)

    assert "<" not in text and "&" not in text
    assert "https://mail.google.com" in text, "the source link survives into plaintext"


# ── delivery ──────────────────────────────────────────────────────────────


def test_the_brief_is_never_cc_d(settings: Settings) -> None:
    """docs/08: "The brief is sent to one address, configured, and never CC'd."""
    multi = settings.model_copy(
        update={"brief_to": "a@x.com,b@y.com", "resend_api_key": "k", "brief_from": "f@x.com"}
    )
    with pytest.raises(deliver.DeliveryError, match="never CC"):
        deliver.Sender(multi).send(subject="s", html="<p>h</p>", text="t")


def test_delivery_refuses_to_start_without_configuration(settings: Settings) -> None:
    with pytest.raises(deliver.DeliveryError, match="BRIEF_TO"):
        deliver.Sender(settings).send(subject="s", html="h", text="t")


def test_the_subject_is_declarative_and_says_when_it_is_incomplete() -> None:
    """docs/05 §Tone: no greeting, no encouragement."""
    assert deliver.subject_for("2026-07-30") == "Backglass — 2026-07-30"
    assert "incomplete" in deliver.subject_for("2026-07-30", degraded=True)


def test_nothing_in_the_delivery_path_can_reach_the_gmail_connector() -> None:
    """docs/10: keeping send capability out of the Gmail credential makes "this system
    cannot email anyone as me" true by construction rather than by discipline."""
    from pathlib import Path

    source = Path(deliver.__file__).read_text()
    assert "connectors" not in source.split('"""', 2)[2]


def test_a_fully_booked_day_says_so_rather_than_showing_an_empty_plan(
    conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """docs/05 §Failure states. A day of back-to-back fixed events renders as a full Today
    section and an empty everything-else, which reads as a normal day. It is not one."""
    conn.execute(
        "INSERT INTO day_plan (user_id, local_date, tz, capacity_minutes, planned_minutes, "
        " overflow_count, generated_at) VALUES (?, ?, 'America/Phoenix', 0, 0, 4, ?)",
        (USER_ID, TODAY.isoformat(), now_iso()),
    )
    plan_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title) "
        "VALUES (?, ?, ?, 'fixed', 'Board prep')",
        (plan_id, f"{TODAY}T09:00:00-07:00", f"{TODAY}T18:00:00-07:00"),
    )
    brief = daily.build(conn, settings, TODAY)

    attention = brief.ordered()[0]
    assert attention.title == "Attention"
    assert "Fully booked" in attention.lines[0].text
    assert "4 items did not fit" in attention.lines[0].text


def test_a_day_with_a_protected_block_is_not_flagged_as_fully_booked(
    conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        "INSERT INTO day_plan (user_id, local_date, tz, capacity_minutes, planned_minutes, "
        " generated_at) VALUES (?, ?, 'America/Phoenix', 480, 120, ?)",
        (USER_ID, TODAY.isoformat(), now_iso()),
    )
    plan_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title) "
        "VALUES (?, ?, ?, 'protected', 'Deep work')",
        (plan_id, f"{TODAY}T09:00:00-07:00", f"{TODAY}T11:00:00-07:00"),
    )
    brief = daily.build(conn, settings, TODAY)
    assert not any(s.title == "Attention" for s in brief.sections)


# ── Plans: the week's arrangements, and the invitations still unanswered ─────


def seed_plan(  # type: ignore[no-untyped-def]
    conn,
    settings: Settings,
    *,
    what: str,
    starts_at: str | None,
    status: str = "confirmed",
    people: tuple[str, ...] = ("Priya Raman <priya@example.com>",),
    confidence: float = 0.9,
    location: str | None = None,
    n: int = 90,
) -> int:
    ledger = Ledger(conn, settings)
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict) "
        "VALUES (?, 'gmail:personal', ?, ?, '2026-07-28T09:00:00-07:00', "
        " 'Priya <priya@example.com>', 'dinner', 'b', '{}', ?, 'keep')",
        (USER_ID, f"p{n}", now_iso(), f"planhash{n}"),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    cursor = conn.execute(
        "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at, when_is_explicit,"
        " location, status, confidence, source_item_id, created_at) "
        "VALUES (?, 'social', ?, ?, NULL, 1, ?, ?, ?, ?, ?)",
        (USER_ID, what, starts_at, location, status, confidence, source_id, now_iso()),
    )
    engagement_id = int(cursor.lastrowid or 0)
    for raw in people:
        entity_id = ledger.resolve_entity(raw)
        if entity_id is not None:
            conn.execute(
                "INSERT INTO engagement_person (user_id, engagement_id, entity_id) "
                "VALUES (?, ?, ?)",
                (USER_ID, engagement_id, entity_id),
            )
    return engagement_id


def plan_lines(conn, settings: Settings) -> list[str]:  # type: ignore[no-untyped-def]
    section = daily.engagement_section(conn, TODAY, settings)
    return [line.text for line in section.lines]


class TestPlansSection:
    def test_a_confirmed_plan_reads_as_a_reminder(  # type: ignore[no-untyped-def]
        self, conn, settings: Settings
    ) -> None:
        seed_plan(
            conn,
            settings,
            what="dinner",
            starts_at="2026-07-31",
            location="Ravi's",
        )
        (text,) = plan_lines(conn, settings)
        assert text == "dinner with Priya Raman at Ravi's. tomorrow."

    def test_an_unanswered_invitation_asks_for_a_reply(  # type: ignore[no-untyped-def]
        self, conn, settings: Settings
    ) -> None:
        """The failure this record type exists to catch: an invitation nobody answers."""
        seed_plan(conn, settings, what="lunch", starts_at="2026-08-03", status="proposed")
        (text,) = plan_lines(conn, settings)
        assert text.startswith("Reply — ")

    def test_unanswered_invitations_lead_the_section(  # type: ignore[no-untyped-def]
        self, conn, settings: Settings
    ) -> None:
        """A confirmed plan is a reminder; an unanswered one is a question, and only the
        question can be acted on by reading it."""
        seed_plan(conn, settings, what="dinner", starts_at="2026-07-31", n=1)
        seed_plan(
            conn, settings, what="squash", starts_at="2026-08-02", status="proposed", n=2
        )
        first, second = plan_lines(conn, settings)
        assert first.startswith("Reply — squash")
        assert not second.startswith("Reply — ")

    def test_a_plan_with_no_date_still_appears(  # type: ignore[no-untyped-def]
        self, conn, settings: Settings
    ) -> None:
        """"We should get dinner sometime" is the plan that decays silently — nothing
        else in the brief would ever surface it."""
        seed_plan(conn, settings, what="dinner sometime", starts_at=None, status="proposed")
        (text,) = plan_lines(conn, settings)
        assert "no date yet" in text

    def test_an_evening_plan_is_reported_on_its_own_local_day(  # type: ignore[no-untyped-def]
        self, conn, settings: Settings
    ) -> None:
        """The 2026-08-01 lesson, in the shape it actually bites.

        A 19:00 Phoenix plan is 02:00 the next day in UTC, so any comparison that lets
        SQLite normalise the offset first reports it a day late. The hour is chosen
        inside the broken window, not near it: a midday fixture passes against the
        broken query too and would prove nothing.
        """
        seed_plan(conn, settings, what="dinner", starts_at="2026-07-30T19:00:00-07:00")
        (text,) = plan_lines(conn, settings)
        assert text.endswith("today.")

    def test_a_low_confidence_plan_stays_out_of_the_brief(  # type: ignore[no-untyped-def]
        self, conn, settings: Settings
    ) -> None:
        """Rule 2: it belongs in the review queue, never in the brief as fact."""
        seed_plan(conn, settings, what="maybe drinks", starts_at="2026-07-31", confidence=0.3)
        assert plan_lines(conn, settings) == []

    def test_a_plan_beyond_the_horizon_is_not_todays_news(  # type: ignore[no-untyped-def]
        self, conn, settings: Settings
    ) -> None:
        seed_plan(conn, settings, what="conference", starts_at="2027-03-01")
        assert plan_lines(conn, settings) == []

    def test_every_plan_line_carries_its_source(  # type: ignore[no-untyped-def]
        self, conn, settings: Settings
    ) -> None:
        """Rule 1, on the new section as on every other."""
        seed_plan(conn, settings, what="dinner", starts_at="2026-07-31")
        section = daily.engagement_section(conn, TODAY, settings)
        assert all(line.provenance is not None for line in section.lines)

    def test_an_evening_plan_on_the_horizon_day_is_still_inside_the_horizon(  # type: ignore[no-untyped-def]
        self, conn, settings: Settings
    ) -> None:
        """The same lesson, one layer down, where SQL does the comparing.

        The horizon is the 14th day out. A plan at 19:00 local on that very day is
        02:00 the day *after* the horizon once SQLite normalises the offset to UTC, so a
        date()-based filter drops it — the owner's last day of visible plans quietly
        loses its evening. Placed exactly on the boundary and in the evening, because
        neither alone reproduces it.
        """
        horizon_day = TODAY + timedelta(days=daily.FRIEND_PLANS_HORIZON_DAYS)
        seed_plan(
            conn,
            settings,
            what="farewell dinner",
            starts_at=f"{horizon_day.isoformat()}T19:00:00-07:00",
        )
        assert plan_lines(conn, settings) != []

    def test_a_plan_restating_a_commitment_is_not_said_twice(
        self, conn, settings: Settings
    ) -> None:
        """B4 across record types.

        Everything read before the engagements prompt existed was filed as a commitment
        or not at all, so obligation-shaped plans sit open on the board while
        re-extraction now also files them as plans. One fact, one line.
        """
        seed(conn, settings, [{"direction": "i_owe", "what": "ASU dorm move-in",
                               "due_at": "2026-07-31"}])
        source_id = int(conn.execute("SELECT id FROM source_item").fetchone()["id"])
        conn.execute(
            "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at,"
            " when_is_explicit, location, status, confidence, source_item_id, created_at)"
            " VALUES (1, 'professional', 'ASU dorm move in', '2026-07-31', NULL, 1, NULL,"
            " 'confirmed', 0.9, ?, ?)",
            (source_id, now_iso()),
        )
        assert plan_lines(conn, settings) == []

    def test_two_different_facts_from_one_message_both_survive(
        self, conn, settings: Settings
    ) -> None:
        """The case the wording check exists to protect: "I'll send the draft Thursday,
        and lunch Friday?" is a commitment and a plan about two different things."""
        seed(conn, settings, [{"direction": "i_owe", "what": "send the draft",
                               "due_at": "2026-07-30"}])
        source_id = int(conn.execute("SELECT id FROM source_item").fetchone()["id"])
        conn.execute(
            "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at,"
            " when_is_explicit, location, status, confidence, source_item_id, created_at)"
            " VALUES (1, 'social', 'lunch', '2026-07-31', NULL, 1, NULL,"
            " 'confirmed', 0.9, ?, ?)",
            (source_id, now_iso()),
        )
        assert len(plan_lines(conn, settings)) == 1

    def test_a_plan_whose_day_has_passed_stops_being_news(
        self, conn, settings: Settings
    ) -> None:
        """Nothing in the codebase can move a plan to `done` — no CLI, no route, and the
        status machine cannot reach it — so with only an upper bound every plan the owner
        ever made stayed in this section forever, reported as "was 933d ago". docs/05
        specifies a brief read in under two minutes."""
        seed_plan(conn, settings, what="last year's dinner", starts_at="2024-01-05")
        assert plan_lines(conn, settings) == []

    def test_an_undated_plan_ages_out_on_the_message_that_proposed_it(
        self, conn, settings: Settings
    ) -> None:
        """"We should get dinner sometime" earns a nag — it is the plan most likely to
        decay — but not an indefinite one."""
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, author, title, body_text, raw_json, content_hash,"
            " triage_verdict) VALUES (?, 'gmail:personal', 'old', ?,"
            " '2024-02-02T09:00:00-07:00', 'Priya', 'dinner', 'b', '{}', 'oldhash','keep')",
            (USER_ID, now_iso()),
        )
        source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at,"
            " when_is_explicit, location, status, confidence, source_item_id, created_at)"
            " VALUES (1, 'social', 'dinner sometime', NULL, NULL, 0, NULL, 'proposed',"
            " 0.9, ?, ?)",
            (source_id, now_iso()),
        )
        assert plan_lines(conn, settings) == []

    def test_a_recent_undated_plan_is_still_shown(self, conn, settings: Settings) -> None:
        seed_plan(conn, settings, what="dinner sometime", starts_at=None, status="proposed")
        assert plan_lines(conn, settings) != []


class TestPlansNeedingReview:
    def review_lines(self, conn, settings: Settings) -> list[str]:  # type: ignore[no-untyped-def]
        return [line.text for line in daily.review_section(conn, TODAY, settings).lines]

    def test_a_low_confidence_plan_reaches_the_review_queue(
        self, conn, settings: Settings
    ) -> None:
        """Rule 2 has two clauses. Engagements honoured "never into the brief as fact"
        from the start and silently dropped "goes to a review queue", because every
        review surface read FROM commitment — while post-processing counted the entry and
        the CLI printed the count. A guess the owner can never see is invisible work
        reported as done."""
        seed_plan(conn, settings, what="maybe drinks", starts_at="2026-08-01",
                  confidence=0.3)
        lines = self.review_lines(conn, settings)
        assert any("maybe drinks" in text for text in lines), lines
        assert any("30% confident" in text for text in lines)

    def test_a_believed_plan_is_not_asked_about(self, conn, settings: Settings) -> None:
        seed_plan(conn, settings, what="confirmed dinner", starts_at="2026-08-01",
                  confidence=0.95)
        assert self.review_lines(conn, settings) == []
