"""People read layer. Phase 6 S1.

The named cases from the plan: search hits every LIKE surface, going-cold fires only
for curated profiles, timeline rows all carry source_item provenance.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

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


class TestMergeCarriesPlans:
    def test_merging_a_duplicate_person_keeps_their_plans(
        self, conn: sqlite3.Connection
    ) -> None:
        """Migration 0014 gave `engagement_person` a NOT NULL foreign key to entity with
        no ON DELETE, and merge() repointed commitments and then deleted the loser — so
        merging anyone who appeared in a single plan failed the constraint, rolled the
        whole merge back, and reached the owner as a 500. Duplicate-person merge is the
        main curation action on the People page, and it worked before engagements landed.
        """
        from backglass.people import merge as merge_mod

        winner = _entity(conn, "Priya Raman")
        loser = _entity(conn, "P. Raman")
        _plan(conn, [loser], what="squash", starts_at="2026-08-05")

        result = merge_mod.merge(conn, winner, loser)

        assert result["plans_repointed"] == 1
        carried = profiles.plans(conn, winner, TODAY.isoformat())["upcoming"]
        assert [p["what"] for p in carried] == ["squash"]

    def test_a_plan_they_both_attended_does_not_break_the_unique_guest_list(
        self, conn: sqlite3.Connection
    ) -> None:
        """The guest list is a set. Repointing the loser onto a plan the winner is
        already in would collide with UNIQUE (user_id, engagement_id, entity_id)."""
        from backglass.people import merge as merge_mod

        winner = _entity(conn, "Priya Raman")
        loser = _entity(conn, "P. Raman")
        _plan(conn, [winner, loser], what="dinner", starts_at="2026-08-05")

        merge_mod.merge(conn, winner, loser)

        rows = conn.execute("SELECT COUNT(*) AS n FROM engagement_person").fetchone()
        assert rows["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM entity").fetchone()["n"] == 1


class TestPlanOrdering:
    @pytest.mark.parametrize("evening_first", [True, False])
    def test_an_evening_plan_sorts_by_its_own_local_day(
        self, conn: sqlite3.Connection, evening_first: bool
    ) -> None:
        """people_plans.sql orders on the local date prefix. Under date() an
        offset-bearing evening plan collates into the next day and ties with a bare-date
        plan that really is later.

        Parametrized on insertion order, and that is the entire point. The first version
        of this test inserted the evening plan first, so when date() produced a tie the
        `e.id DESC` tie-break happened to yield the right answer and the test stayed green
        against the bug it was written for — the same false proof it was written to
        replace. One of these two orders fails under date(); a single order proves nothing.
        """
        priya = _entity(conn, "Priya Raman")
        evening = ("evening of the 5th", "2026-08-05T19:00:00-07:00")
        allday = ("all day the 6th", "2026-08-06")
        first, second = (evening, allday) if evening_first else (allday, evening)
        _plan(conn, [priya], what=first[0], starts_at=first[1], n=1)
        _plan(conn, [priya], what=second[0], starts_at=second[1], n=2)

        ahead = profiles.plans(conn, priya, TODAY.isoformat())["upcoming"]
        upcoming = [p["what"] for p in ahead]

        assert upcoming == ["evening of the 5th", "all day the 6th"]

    def test_every_table_that_names_an_entity_survives_a_merge(
        self, conn: sqlite3.Connection
    ) -> None:
        """The invariant, asserted mechanically instead of in prose.

        merge() repoints references and then deletes the loser, so a table added later
        that names an entity and is not repointed turns the People page's main action
        into a 500 — which is exactly how `engagement_person` broke it, and how
        `activity.contact_entity_id` and `entity_merge.winner_id` had been broken since
        before that. Derived from the live schema rather than a hand-written list, so the
        next table to forget fails here and not in the owner's browser.
        """
        from backglass.people import merge as merge_mod

        referencing = {
            (str(row["name"]), str(fk["from"]))
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
            for fk in conn.execute(f"PRAGMA foreign_key_list({row['name']})")
            if str(fk["table"]) == "entity"
        }
        assert referencing, "the schema should still have foreign keys to entity"

        winner = _entity(conn, "Survivor")
        for table, column in sorted(referencing):
            loser = _entity(conn, f"Dupe for {table}.{column}")
            _seed_reference(conn, table, column, loser)
            merge_mod.merge(conn, winner, loser)
            left = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?", (loser,)
            ).fetchone()
            assert left["n"] == 0, f"{table}.{column} still points at the deleted entity"


def _seed_reference(
    conn: sqlite3.Connection, table: str, column: str, entity_id: int
) -> None:
    """One row in `table` whose `column` names `entity_id`, whatever the table is."""
    if table == "engagement_person":
        _plan(conn, [entity_id], what=f"plan {entity_id}", starts_at="2026-08-05",
              n=1000 + entity_id)
        return
    if table == "commitment":
        _interaction(conn, entity_id, f"2026-07-1{entity_id % 10}T09:00:00-07:00")
        return
    if table == "entity_merge":
        conn.execute(
            "INSERT INTO entity_merge (user_id, winner_id, loser_snapshot_json, merged_at)"
            " VALUES (1, ?, '{}', '2026-07-01T00:00:00Z')",
            (entity_id,),
        )
        return
    if table == "activity":
        conn.execute(
            "INSERT INTO activity (user_id, title, category, contact_entity_id, created_at)"
            " VALUES (1, ?, 'research', ?, '2026-07-01T00:00:00Z')",
            (f"activity {entity_id}", entity_id),
        )
        return
    raise AssertionError(
        f"{table}.{column} references entity and this test does not know how to seed it — "
        "add a case so the merge invariant covers it"
    )


class TestGuessesAreMarkedOnTheProfile:
    def test_a_low_confidence_plan_is_not_stated_as_fact(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The brief and the capacity model both honour rule 2 for plans; the person page
        rendered a 0.30-confidence plan identically to a certain one — plain text, no
        marker — which is a guess stated as fact on the surface most likely to be read as
        a record of the relationship."""
        from fastapi.testclient import TestClient

        from backglass.web.app import create_app

        priya = _entity(conn, "Priya Raman")
        _plan(conn, [priya], what="shaky dinner", starts_at="2099-08-05", n=77)
        conn.execute("UPDATE engagement SET confidence = 0.3 WHERE what = 'shaky dinner'")

        client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
        body = client.get(f"/people/{priya}").text

        assert "shaky dinner" in body
        assert "unconfirmed guess" in body
