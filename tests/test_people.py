"""People read layer. Phase 6 S1.

The named cases from the plan: search hits every LIKE surface, going-cold fires only
for curated profiles, timeline rows all carry source_item provenance.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from backglass.config import Settings
from backglass.people import profiles, touch

TODAY = date(2026, 7, 30)


def _entity(
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


def _interaction(
    conn: sqlite3.Connection, entity_id: int, occurred: str, *, author: str = "x@example.com"
) -> int:
    cur = conn.execute(
        "INSERT INTO source_item (source, external_id, fetched_at, occurred_at, author,"
        " title, body_text, content_hash, triage_verdict)"
        " VALUES ('gmail:personal', ?, ?, ?, ?, 'Subject', 'body', ?, 'keep')",
        (f"m-{entity_id}-{occurred}", occurred, occurred, author, f"h-{entity_id}-{occurred}"),
    )
    sid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO commitment (direction, counterparty_entity_id, what, confidence,"
        " source_item_id, created_at) VALUES ('i_owe', ?, 'thing', 0.9, ?, ?)",
        (entity_id, sid, occurred),
    )
    return sid


class TestSearch:
    def test_matches_name_alias_role_org_and_tag(self, conn: sqlite3.Connection) -> None:
        _entity(conn, "Ravi Menon", aliases=["ravi@stellarcap.vc"], role="Partner",
                org="Stellar Capital", tags=["investor"])
        assert len(profiles.search(conn, q="ravi")) == 1          # name
        assert len(profiles.search(conn, q="stellarcap.vc")) == 1  # alias
        assert len(profiles.search(conn, q="partner")) == 1        # role
        assert len(profiles.search(conn, q="stellar")) == 1        # org
        assert len(profiles.search(conn, q="investor")) == 1       # tag text
        assert profiles.search(conn, q="nobody-by-this-name") == []

    def test_tag_filter_is_exact_not_substring(self, conn: sqlite3.Connection) -> None:
        _entity(conn, "A", tags=["investor"])
        _entity(conn, "B", tags=["investor-relations"])
        hits = profiles.search(conn, tag="investor")
        assert [h["canonical_name"] for h in hits] == ["A"]

    def test_empty_query_lists_everyone(self, conn: sqlite3.Connection) -> None:
        _entity(conn, "A")
        _entity(conn, "B")
        assert len(profiles.search(conn)) == 2

    def test_search_excludes_projects(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO entity (kind, canonical_name) VALUES ('project', 'Backglass')"
        )
        assert profiles.search(conn) == []


class TestTimeline:
    def test_every_row_carries_provenance(self, conn: sqlite3.Connection) -> None:
        eid = _entity(conn, "Ravi Menon", aliases=["ravi@stellarcap.vc"])
        _interaction(conn, eid, "2026-07-01T10:00:00-07:00")
        rows = profiles.timeline(conn, eid)
        assert rows
        for row in rows:
            assert row["source"] and row["source_external_id"] and row["source_occurred_at"]

    def test_author_mention_appears_once_not_twice(self, conn: sqlite3.Connection) -> None:
        # A source item that is both commitment evidence and author-matched must not
        # duplicate across the two UNION legs.
        eid = _entity(conn, "Ravi Menon", aliases=["ravi@stellarcap.vc"])
        _interaction(conn, eid, "2026-07-01T10:00:00-07:00", author="ravi@stellarcap.vc")
        rows = profiles.timeline(conn, eid)
        assert len([r for r in rows if r["via"] == "commitment"]) == 1
        assert [r for r in rows if r["via"] == "mention"] == []

    def test_authored_mail_without_commitment_is_a_mention(
        self, conn: sqlite3.Connection
    ) -> None:
        eid = _entity(conn, "Ravi Menon", aliases=["ravi@stellarcap.vc"])
        conn.execute(
            "INSERT INTO source_item (source, external_id, fetched_at, occurred_at,"
            " author, title, body_text, content_hash, triage_verdict)"
            " VALUES ('gmail:personal', 'm-x', '2026-07-02', '2026-07-02T09:00:00-07:00',"
            " 'Ravi Menon <ravi@stellarcap.vc>', 'FYI', 'no ask', 'h-x', 'drop')",
        )
        rows = profiles.timeline(conn, eid)
        assert [r["via"] for r in rows] == ["mention"]


class TestTouch:
    def test_cold_fires_only_for_curated_profiles(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        curated = _entity(conn, "Ravi Menon", role="Partner")
        drive_by = _entity(conn, "Random Sender")
        _interaction(conn, curated, "2026-04-01T10:00:00-07:00")
        _interaction(conn, drive_by, "2026-04-01T10:00:00-07:00")
        touches = touch.cold(conn, settings, TODAY)
        assert [t.entity_id for t in touches] == [curated]
        assert touches[0].level == "cold"

    def test_levels_follow_settings_thresholds(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        fresh = _entity(conn, "F", role="x")
        warm = _entity(conn, "W", role="x")
        _interaction(conn, fresh, "2026-07-25T10:00:00-07:00")   # 5 days
        _interaction(conn, warm, "2026-06-20T10:00:00-07:00")    # 40 days
        levels = {t.name: t.level for t in touch.cold(conn, settings, TODAY)}
        assert levels == {"F": "fresh", "W": "warn"}

    def test_chip_is_a_day_count_never_a_bare_state(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        eid = _entity(conn, "W", role="x")
        _interaction(conn, eid, "2026-06-20T10:00:00-07:00")
        (t,) = touch.cold(conn, settings, TODAY)
        assert t.chip() == "40 days since last touch"

    def test_never_contacted_curated_profile_has_no_brief_provenance(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _entity(conn, "New Contact", role="Recruiter")
        all_touches = touch.cold(conn, settings, TODAY)
        assert len(all_touches) == 1 and all_touches[0].days_since is None
        # Surfaced on the People page, but never eligible for a brief line — a claim
        # with no source item cannot ship (CLAUDE.md rule 1).
        assert touch.needing_follow_up(conn, settings, TODAY) == []


class TestFollowUpSection:
    def _cold_profile(self, conn: sqlite3.Connection, name: str = "Ravi Menon") -> int:
        eid = _entity(conn, name, role="Partner", org="Stellar Capital")
        _interaction(conn, eid, "2026-04-01T10:00:00-07:00")
        return eid

    def test_omitted_when_nobody_is_cold(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.brief import daily

        brief = daily.build(conn, settings, for_date=TODAY)
        assert all(s.title != "Follow up" for s in brief.sections)

    def test_lines_capped_at_three_with_provenance(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.brief import daily

        for n in range(5):
            self._cold_profile(conn, f"Person {n}")
        section = daily.follow_up_section(conn, TODAY, settings)
        assert len(section.lines) == 3
        for line in section.lines:
            assert line.provenance is not None
            assert "days since last touch" in line.text

    def test_brief_word_cap_holds_with_the_new_section(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.brief import daily

        for n in range(3):
            self._cold_profile(conn, f"Person {n}")
        brief = daily.build(conn, settings, for_date=TODAY)
        assert brief.word_count() <= 400
        assert any(s.title == "Follow up" for s in brief.sections)


# ── plans and the derived profile ───────────────────────────────────────────


def _plan(
    conn: sqlite3.Connection,
    entity_ids: list[int],
    *,
    what: str,
    starts_at: str | None,
    kind: str = "social",
    status: str = "confirmed",
    source: str = "imessage",
    n: int = 1,
) -> int:
    cur = conn.execute(
        "INSERT INTO source_item (source, external_id, fetched_at, occurred_at, author,"
        " title, body_text, content_hash, triage_verdict)"
        " VALUES (?, ?, '2026-07-20T09:00:00-07:00', '2026-07-20T09:00:00-07:00',"
        " 'someone', 'msg', 'body', ?, 'keep')",
        (source, f"plan-{n}", f"planhash-{n}"),
    )
    sid = int(cur.lastrowid)
    cur = conn.execute(
        "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at, when_is_explicit,"
        " location, status, confidence, source_item_id, created_at)"
        " VALUES (1, ?, ?, ?, NULL, 1, NULL, ?, 0.9, ?, '2026-07-20T09:00:00-07:00')",
        (kind, what, starts_at, status, sid),
    )
    engagement_id = int(cur.lastrowid)
    for entity_id in entity_ids:
        conn.execute(
            "INSERT INTO engagement_person (user_id, engagement_id, entity_id)"
            " VALUES (1, ?, ?)",
            (engagement_id, entity_id),
        )
    return engagement_id


class TestPlans:
    def test_upcoming_and_past_are_split_on_the_local_day(
        self, conn: sqlite3.Connection
    ) -> None:
        priya = _entity(conn, "Priya Raman")
        _plan(conn, [priya], what="old dinner", starts_at="2026-07-01", n=1)
        _plan(conn, [priya], what="next dinner", starts_at="2026-08-05", n=2)

        split = profiles.plans(conn, priya, TODAY.isoformat())

        assert [p["what"] for p in split["upcoming"]] == ["next dinner"]
        assert [p["what"] for p in split["past"]] == ["old dinner"]

    def test_an_evening_plan_today_is_still_upcoming(self, conn: sqlite3.Connection) -> None:
        """The offset-normalisation trap, at the boundary where it bites: 19:00 in
        Phoenix is the next day in UTC, and anything that lets SQLite convert first
        files tonight's dinner under history."""
        priya = _entity(conn, "Priya Raman")
        _plan(conn, [priya], what="dinner", starts_at=f"{TODAY.isoformat()}T19:00:00-07:00")
        split = profiles.plans(conn, priya, TODAY.isoformat())
        assert [p["what"] for p in split["upcoming"]] == ["dinner"]

    def test_a_declined_plan_is_history_whatever_its_date(
        self, conn: sqlite3.Connection
    ) -> None:
        priya = _entity(conn, "Priya Raman")
        _plan(conn, [priya], what="drinks", starts_at="2026-08-05", status="declined")
        split = profiles.plans(conn, priya, TODAY.isoformat())
        assert split["upcoming"] == []
        assert [p["what"] for p in split["past"]] == ["drinks"]

    def test_a_shared_plan_names_the_other_guests_not_the_subject(
        self, conn: sqlite3.Connection
    ) -> None:
        priya = _entity(conn, "Priya Raman")
        sam = _entity(conn, "Sam Ellis")
        _plan(conn, [priya, sam], what="dinner", starts_at="2026-08-05")

        (row,) = profiles.plans(conn, priya, TODAY.isoformat())["upcoming"]
        assert row["others"] == "Sam Ellis"


class TestDerivedProfile:
    def test_the_lean_follows_the_evidence(self, conn: sqlite3.Connection) -> None:
        priya = _entity(conn, "Priya Raman")
        _plan(conn, [priya], what="dinner", starts_at="2026-08-05", kind="social", n=1)
        _plan(conn, [priya], what="lab meeting", starts_at="2026-08-06",
              kind="professional", n=2)

        record = profiles.derived(conn, priya, TODAY.isoformat())

        assert record["lean"] == "both"
        assert record["social_count"] == 1
        assert record["professional_count"] == 1

    def test_channels_count_both_commitments_and_plans(
        self, conn: sqlite3.Connection
    ) -> None:
        """How the owner knows someone is a fact about where they talk, and the two
        record types are two halves of the same answer."""
        priya = _entity(conn, "Priya Raman")
        _interaction(conn, priya, "2026-07-10T09:00:00-07:00")  # gmail, via a commitment
        _plan(conn, [priya], what="dinner", starts_at="2026-08-05", source="imessage")

        record = profiles.derived(conn, priya, TODAY.isoformat())

        sources = {c["source"]: c["count"] for c in record["channels"]}
        assert sources == {"gmail:personal": 1, "imessage": 1}
        assert record["mentions"] == 2

    def test_someone_with_no_evidence_yet_reads_as_empty_not_as_an_error(
        self, conn: sqlite3.Connection
    ) -> None:
        """A manually created profile has nothing behind it, and the page still renders."""
        alone = _entity(conn, "Nobody Yet")
        record = profiles.derived(conn, alone, TODAY.isoformat())
        assert record["mentions"] == 0
        assert record["lean"] is None
        assert record["channels"] == []
