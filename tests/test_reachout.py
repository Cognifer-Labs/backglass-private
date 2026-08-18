"""Reach-out drafts: the email that keeps an in-person connection warm.

The cases that matter are the ones with no evidence behind them. Someone met once at a
dinner has no thread, no commitment and no last touch, and the draft still has to be a
complete, sendable email — that is the whole reason this exists rather than being a
report over the ledger.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

import pytest

from backglass.config import Settings
from backglass.facts import remember
from backglass.people import reachout

TODAY = date(2026, 8, 12)

NOTE = (
    "I especially appreciated you answering my questions about how immune cells tell "
    "cancerous cells apart from healthy ones."
)


def _person(
    conn: sqlite3.Connection,
    name: str,
    *,
    aliases: list[str] | None = None,
    role: str | None = None,
    org: str | None = None,
    tags: list[str] | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO entity (kind, canonical_name, aliases_json, role, org, tags_json)"
        " VALUES ('person', ?, ?, ?, ?, ?)",
        (name, json.dumps(aliases or []), role, org, json.dumps(tags or [])),
    )
    return int(cur.lastrowid)


def _interaction(conn: sqlite3.Connection, entity_id: int, occurred: str) -> int:
    """A commitment with a source item — what `people_cold.sql` reads as a touch."""
    cur = conn.execute(
        "INSERT INTO source_item (source, external_id, fetched_at, occurred_at, author,"
        " title, body_text, content_hash, triage_verdict)"
        " VALUES ('apple-mail', ?, ?, ?, 'them@example.com', 'Subject', 'body', ?, 'keep')",
        (f"m-{entity_id}-{occurred}", occurred, occurred, f"h-{entity_id}-{occurred}"),
    )
    sid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO commitment (direction, counterparty_entity_id, what, confidence,"
        " source_item_id, created_at) VALUES ('i_owe', ?, 'thing', 0.9, ?, ?)",
        (entity_id, sid, occurred),
    )
    return sid


def _owner(conn: sqlite3.Connection, settings: Settings) -> None:
    remember(conn, settings, "identity", "name", "Dharsan Kesavan")
    remember(
        conn, settings, "identity", "emails",
        "contactdharsan@gmail.com (personal) · dkesava2@asu.edu (ASU)",
    )


class TestNoEvidence:
    """The primary case: met in person, nothing in the ledger."""

    def test_renders_a_complete_email_from_name_and_note(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _owner(conn, settings)
        pid = _person(conn, "Felipe Batalini", tags=["connection"])

        record = reachout.draft(
            conn, settings, pid, note=NOTE, template="thanks",
            where="at the McKenna dinner", day=TODAY,
        )

        assert record.body.startswith("Hi Felipe,")
        assert NOTE in record.body
        assert "at the McKenna dinner" in record.body
        assert record.subject == "Thank you for your time, Felipe"
        assert record.body.rstrip().endswith("Dharsan Kesavan\ncontactdharsan@gmail.com")
        # Nothing half-filled reaches the owner.
        assert "{" not in record.body and "}" not in record.body

    def test_no_touch_means_no_invented_gap(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        record = reachout.draft(conn, settings, pid, note=NOTE, template="warm", day=TODAY)

        assert "days" not in record.body
        assert "It's been a while since we last spoke and" in record.body
        assert any("gap: omitted" in line for line in record.evidence)

    def test_where_is_optional_and_leaves_no_seam(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        record = reachout.draft(conn, settings, pid, note=NOTE, day=TODAY)

        assert "Thank you for taking the time to talk. " + NOTE in record.body
        assert "  " not in record.body

    def test_missing_owner_facts_degrade_to_an_unsigned_draft(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """No identity fact must never mean an invented signature (rule 1)."""
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        record = reachout.draft(conn, settings, pid, note=NOTE, day=TODAY)

        assert record.body.rstrip().endswith("Best,")
        assert not any("sign-off" in line for line in record.evidence)


class TestLedgerEnrichment:
    def test_gap_states_the_measured_number_and_cites_the_row(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Ravi Menon", role="Partner")
        sid = _interaction(conn, pid, "2026-06-13T10:00:00Z")

        record = reachout.draft(conn, settings, pid, note=NOTE, template="warm", day=TODAY)

        assert "about 60 days" in record.body
        assert any(f"source_item #{sid}" in line for line in record.evidence)

    def test_org_appears_only_when_the_profile_carries_one(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        with_org = _person(conn, "Ravi Menon", org="Stellar Capital", tags=["investor"])
        without = _person(conn, "Ana Silva", tags=["connection"])

        assert "at Stellar Capital" in reachout.draft(
            conn, settings, with_org, note=NOTE, template="ask", day=TODAY
        ).body
        assert " at " not in reachout.draft(
            conn, settings, without, note=NOTE, template="ask", day=TODAY
        ).body


class TestGuards:
    def test_a_draft_without_a_note_is_refused(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        with pytest.raises(reachout.ReachoutError, match="--note"):
            reachout.draft(conn, settings, pid, note="   ", day=TODAY)

    def test_unknown_template_names_the_known_ones(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        with pytest.raises(reachout.ReachoutError, match="thanks"):
            reachout.draft(conn, settings, pid, note=NOTE, template="cold-blast", day=TODAY)

    def test_every_template_survives_a_bare_profile(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        for name in reachout.TEMPLATES:
            record = reachout.draft(conn, settings, pid, note=NOTE, template=name, day=TODAY)
            assert "{" not in record.body and "{" not in record.subject
            assert NOTE in record.body
            assert record.subject.strip()

    def test_ambiguous_name_refuses_rather_than_guessing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        del settings
        _person(conn, "Felipe Batalini", tags=["connection"])
        _person(conn, "Felipe Moreno", tags=["connection"])
        with pytest.raises(reachout.ReachoutError, match="matches 2 people"):
            reachout.resolve(conn, "felipe")

    def test_exact_name_wins_over_a_partial_sibling(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        del settings
        pid = _person(conn, "Felipe", tags=["connection"])
        _person(conn, "Felipe Batalini", tags=["connection"])
        assert reachout.resolve(conn, "Felipe") == pid

    def test_drafting_writes_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """CLAUDE.md rule 3, in the form that matters here: a report is not a sync."""
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        conn.commit()
        before = conn.total_changes

        reachout.draft(conn, settings, pid, note=NOTE, day=TODAY)
        reachout.candidates(conn, settings, TODAY)

        assert conn.total_changes == before


class TestAddress:
    def test_no_alias_means_no_mailto_and_the_draft_says_so(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        record = reachout.draft(conn, settings, pid, note=NOTE, day=TODAY)

        assert record.to_email is None
        assert record.mailto() is None
        assert any("address: none on the profile" in line for line in record.evidence)

    def test_an_email_alias_becomes_a_mailto(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(
            conn, "Felipe Batalini", aliases=["felipe@example.edu"], tags=["connection"]
        )
        record = reachout.draft(conn, settings, pid, note=NOTE, day=TODAY)

        link = record.mailto() or ""
        assert link.startswith("mailto:felipe%40example.edu?subject=")
        assert "immune%20cells" in link


class TestCandidates:
    def test_a_never_touched_curated_profile_is_a_candidate(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """`touch.needing_follow_up` requires a source row, so it cannot see the person
        this feature is for. The candidate list must."""
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        assert [t.entity_id for t in reachout.candidates(conn, settings, TODAY)] == [pid]

    def test_an_uncurated_profile_is_not_tracked(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _person(conn, "Someone From Extraction")
        assert reachout.candidates(conn, settings, TODAY) == []


class TestPersonPage:
    """The same draft on the surface the owner actually opens."""

    @pytest.fixture
    def client(self, conn: sqlite3.Connection, settings: Settings):  # type: ignore[no-untyped-def]
        from fastapi.testclient import TestClient

        from backglass.web.app import create_app

        del conn  # migrated db on disk; the app opens its own connections
        return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

    def test_the_panel_offers_every_template(
        self, conn: sqlite3.Connection, settings: Settings, client
    ) -> None:  # type: ignore[no-untyped-def]
        del settings
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        conn.commit()

        page = client.get(f"/people/{pid}")

        assert page.status_code == 200
        for name in reachout.TEMPLATES:
            assert f'value="{name}"' in page.text

    def test_posting_a_note_returns_the_draft_with_its_provenance(
        self, conn: sqlite3.Connection, settings: Settings, client
    ) -> None:  # type: ignore[no-untyped-def]
        _owner(conn, settings)
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        conn.commit()
        before = conn.total_changes

        page = client.post(
            f"/people/{pid}/reachout",
            data={"template": "thanks", "note": NOTE, "where": "at the McKenna dinner"},
        )

        assert page.status_code == 200
        assert "Hi Felipe," in page.text
        assert "immune cells" in page.text
        assert "Where each part came from" in page.text
        assert conn.total_changes == before  # a draft is a read

    def test_an_empty_note_answers_in_the_fragment_not_a_500(
        self, conn: sqlite3.Connection, settings: Settings, client
    ) -> None:  # type: ignore[no-untyped-def]
        del settings
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        conn.commit()

        page = client.post(f"/people/{pid}/reachout", data={"template": "thanks", "note": ""})

        assert page.status_code == 200
        assert "--note" in page.text
        assert "Hi Felipe," not in page.text


class TestTouchpoints:
    """The reminder half: a touch the ledger cannot see, recorded so it can be cited."""

    def test_a_recorded_touch_becomes_evidence_the_brief_may_cite(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The whole point. `follow_up_section` refuses a person with no source row, so
        before this a dinner could never produce a nudge."""
        from backglass.people import touch

        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        touch.record(conn, settings, pid, kind="met", occurred_at="2026-06-01",
                     note="McKenna dinner")

        [t] = touch.cold(conn, settings, TODAY)
        assert t.days_since == 72
        assert t.source_row is not None
        assert t.source_row["source"] == "manual"
        assert t.source_row["source_title"] == "Met: Felipe Batalini"
        assert [x.entity_id for x in touch.needing_follow_up(conn, settings, TODAY)] == [pid]

    def test_the_newest_evidence_wins_whichever_kind_it_is(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.people import touch

        pid = _person(conn, "Ravi Menon", role="Partner")
        _interaction(conn, pid, "2026-07-01T10:00:00Z")
        assert touch.cold(conn, settings, TODAY)[0].days_since == 42

        touch.record(conn, settings, pid, kind="call", occurred_at="2026-08-10")
        assert touch.cold(conn, settings, TODAY)[0].days_since == 2

        # And a touch older than the commitment does not rewind the clock.
        touch.record(conn, settings, pid, kind="met", occurred_at="2026-05-01")
        assert touch.cold(conn, settings, TODAY)[0].days_since == 2

    def test_logging_the_same_touch_twice_in_a_day_is_one_touch(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Rule 3, in the only form a hand-entered record can take."""
        from backglass.people import touch

        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        first = touch.record(conn, settings, pid, kind="met", occurred_at="2026-08-11")
        conn.commit()
        before = conn.total_changes

        again = touch.record(conn, settings, pid, kind="met", occurred_at="2026-08-11",
                             note="different words, same meeting")

        assert again == first
        assert conn.total_changes == before
        assert len(touch.history(conn, pid)) == 1

    def test_a_bad_kind_or_date_is_refused_with_a_sentence(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.people import touch

        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        with pytest.raises(touch.TouchError, match="unknown kind"):
            touch.record(conn, settings, pid, kind="vibes")
        with pytest.raises(touch.TouchError, match="not a date"):
            touch.record(conn, settings, pid, occurred_at="last tuesday")

    def test_history_carries_provenance_for_every_row(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.people import touch

        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        touch.record(conn, settings, pid, kind="met", occurred_at="2026-08-01")
        touch.record(conn, settings, pid, kind="sent", occurred_at="2026-08-11")

        rows = touch.history(conn, pid)
        assert [r["kind"] for r in rows] == ["sent", "met"]  # newest first
        assert all(r["source_item_id"] and r["source"] == "manual" for r in rows)


class TestCadence:
    def test_the_owners_cadence_replaces_both_thresholds(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.people import touch

        pid = _person(conn, "Ravi Menon", tags=["connection"])
        touch.set_cadence(conn, pid, 30)

        # Exactly at the cadence is due; one short of it is not.
        touch.record(conn, settings, pid, kind="met", occurred_at="2026-07-13")  # 30 days
        assert touch.cold(conn, settings, TODAY)[0].level == "warn"

        conn.execute("DELETE FROM touchpoint WHERE entity_id = ?", (pid,))
        touch.record(conn, settings, pid, kind="call", occurred_at="2026-07-14")  # 29
        assert touch.cold(conn, settings, TODAY)[0].level == "fresh"

    def test_overdue_is_twice_the_cadence(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.people import touch

        pid = _person(conn, "Ravi Menon", tags=["connection"])
        touch.set_cadence(conn, pid, 30)
        touch.record(conn, settings, pid, kind="met", occurred_at="2026-06-13")  # 60 days

        row = touch.cold(conn, settings, TODAY)[0]
        assert row.level == "cold"
        assert row.cadence_chip() == "every 30 days"

    def test_no_cadence_falls_back_to_the_settings_pair_and_says_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.people import touch

        pid = _person(conn, "Ravi Menon", tags=["connection"])
        touch.record(conn, settings, pid, kind="met",
                     occurred_at=str(TODAY.replace(day=1)))

        row = touch.cold(conn, settings, TODAY)[0]
        assert row.every_days == settings.people_touch_warn_days
        assert row.cadence_is_owners is False
        assert row.cadence_chip() is None

    def test_clearing_a_cadence_hands_the_person_back_to_the_defaults(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.people import touch

        pid = _person(conn, "Ravi Menon", tags=["connection"])
        touch.set_cadence(conn, pid, 7)
        touch.set_cadence(conn, pid, None)
        assert touch.cold(conn, settings, TODAY)[0].cadence_is_owners is False

    def test_setting_the_same_cadence_twice_writes_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.people import touch

        del settings
        pid = _person(conn, "Ravi Menon", tags=["connection"])
        touch.set_cadence(conn, pid, 21)
        conn.commit()
        before = conn.total_changes

        touch.set_cadence(conn, pid, 21)

        assert conn.total_changes == before

    def test_a_cadence_of_zero_is_refused(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.people import touch

        del settings
        pid = _person(conn, "Ravi Menon", tags=["connection"])
        with pytest.raises(touch.TouchError, match="at least 1"):
            touch.set_cadence(conn, pid, 0)


class TestBriefNudge:
    def test_a_person_known_only_from_a_recorded_touch_reaches_the_brief(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The sentence in follow_up_section's docstring, now answerable: the claim has
        a source, so it ships."""
        from backglass.brief.daily import follow_up_section
        from backglass.people import touch

        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        touch.record(conn, settings, pid, kind="met", occurred_at="2026-06-01",
                     note="McKenna dinner")

        section = follow_up_section(conn, TODAY, settings)

        assert len(section.lines) == 1
        line = section.lines[0]
        assert "Felipe Batalini" in line.text
        assert "Met: Felipe Batalini" in line.text
        assert line.provenance is not None
        assert line.provenance.source == "manual"

    def test_a_person_with_no_evidence_at_all_still_stays_out(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Rule 1 is untouched: recording a touch is how you get in, not an exemption."""
        from backglass.brief.daily import follow_up_section

        _person(conn, "Felipe Batalini", tags=["connection"])
        assert follow_up_section(conn, TODAY, settings).lines == []


class TestTouchOnThePage:
    @pytest.fixture
    def client(self, conn: sqlite3.Connection, settings: Settings):  # type: ignore[no-untyped-def]
        from fastapi.testclient import TestClient

        from backglass.web.app import create_app

        del conn
        return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

    def test_recording_a_touch_moves_the_chip(
        self, conn: sqlite3.Connection, settings: Settings, client
    ) -> None:  # type: ignore[no-untyped-def]
        del settings
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        conn.commit()
        assert "no interactions yet" in client.get(f"/people/{pid}").text

        # The page computes days-since against the wall clock, so the fixture date has
        # to be relative — a hardcoded "yesterday" is 1 day old exactly once.
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        posted = client.post(
            f"/people/{pid}/touch",
            data={"kind": "met", "on": yesterday, "note": "McKenna dinner"},
            follow_redirects=False,
        )

        assert posted.status_code == 303
        page = client.get(f"/people/{pid}").text
        assert "1 days since last touch" in page
        assert "McKenna dinner" in page

    def test_a_cadence_typed_on_the_page_changes_the_level(
        self, conn: sqlite3.Connection, settings: Settings, client
    ) -> None:  # type: ignore[no-untyped-def]
        from backglass.people import touch

        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        touch.record(conn, settings, pid, kind="met", occurred_at="2026-08-01")
        conn.commit()

        client.post(f"/people/{pid}/cadence", data={"every_days": "7"},
                    follow_redirects=False)

        page = client.get(f"/people/{pid}").text
        assert "every 7 days" in page
        assert [t.level for t in touch.cold(conn, settings, TODAY)] == ["warn"]

    def test_a_cadence_that_is_not_a_number_answers_in_words(
        self, conn: sqlite3.Connection, settings: Settings, client
    ) -> None:  # type: ignore[no-untyped-def]
        del settings
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        conn.commit()

        answer = client.post(f"/people/{pid}/cadence", data={"every_days": "soon"})

        assert answer.status_code == 422
        assert "not a number of days" in answer.text


class TestTimelineAgreement:
    def test_a_recorded_touch_appears_in_the_timeline(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The banner and the timeline must not disagree: "0 days since last touch"
        above "No interactions on record" is two surfaces contradicting each other about
        the same person."""
        from backglass.people import profiles, touch

        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        touch.record(conn, settings, pid, kind="met", occurred_at="2026-08-11",
                     note="McKenna dinner")

        rows = profiles.timeline(conn, pid)

        assert [r["via"] for r in rows] == ["touch"]
        assert rows[0]["source_title"] == "Met: Felipe Batalini"
        assert rows[0]["what"] == "McKenna dinner"
        assert rows[0]["source_item_id"]

    def test_touches_interleave_with_evidence_newest_first(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.people import profiles, touch

        pid = _person(conn, "Ravi Menon", role="Partner")
        _interaction(conn, pid, "2026-08-05T10:00:00Z")
        touch.record(conn, settings, pid, kind="call", occurred_at="2026-08-09")
        _interaction(conn, pid, "2026-07-01T10:00:00Z")

        assert [r["via"] for r in profiles.timeline(conn, pid)] == [
            "touch", "commitment", "commitment",
        ]
