"""The activity registry and the spaced-repetition sources.

The connector tests build real SQLite fixture files in the shapes Anki and Avorio
actually write — no live store is ever opened (docs/10 §Testing). Where a connector
reads the wall clock (its due snapshot is "today" by definition), the test computes
its expectations from the same clock rather than freezing it; everything downstream
of the connectors takes an explicit `day` and is tested on fixed dates.

The 2026-07-30 cursor lesson is asserted directly: the second fetch must yield
*nothing*, not merely write nothing.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from backglass.config import Settings
from backglass.connectors.anki import AnkiConnector
from backglass.connectors.avorio import AvorioConnector
from backglass.goals import activities, checkpoints
from backglass.goals import reviews as reviews_mod
from backglass.plan import capacity
from backglass.roadmap import instantiate
from backglass.web.app import create_app

TZ = "America/Phoenix"


# ──────────────────────────────────────────────────────────── shared helpers


def _seed_goal_with_target(conn: sqlite3.Connection, kind: str = "cadence") -> tuple[int, int]:
    conn.execute(
        "INSERT INTO goal (user_id, title, horizon, definition_of_done, created_at) "
        "VALUES (1, 'Get into med school', 'annual', 'Matched', '2026-01-01T00:00:00')"
    )
    goal_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO target (goal_id, kind, title, weekly_count, created_at) "
        "VALUES (?, ?, 'Reviews done', 7, '2026-01-01T00:00:00')",
        (goal_id, kind),
    )
    target_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    return goal_id, target_id


def _seed_source_item(
    conn: sqlite3.Connection,
    *,
    source: str,
    external_id: str,
    occurred_at: str,
    raw: dict[str, object],
    title: str = "seeded",
) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at, "
        " author, title, body_text, raw_json, content_hash) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, '', ?, ?)",
        (
            source,
            external_id,
            occurred_at,
            occurred_at,
            source,
            title,
            json.dumps(raw, sort_keys=True),
            f"hash:{source}:{external_id}",
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _review_day(
    conn: sqlite3.Connection, source: str, day: str, reviews: int, minutes: int
) -> int:
    return _seed_source_item(
        conn,
        source=source,
        external_id=f"reviews:{day}:{day}T20:00:00",
        occurred_at=f"{day}T20:00:00+00:00",
        raw={"date": day, "reviews": reviews, "minutes": minutes},
    )


def _due_day(conn: sqlite3.Connection, source: str, day: str, due: int) -> int:
    return _seed_source_item(
        conn,
        source=source,
        external_id=f"due:{day}:{due}",
        occurred_at=f"{day}T00:00:00+00:00",
        raw={"date": day, "due": due},
        title=f"{source} due · {day}",
    )


# ─────────────────────────────────────────────────────────── migration 0007


class TestMigration:
    def test_activity_table_and_checkpoint_column_exist(self, conn: sqlite3.Connection) -> None:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(activity)")}
        assert {"title", "org", "role", "category", "most_meaningful"} <= cols
        ck = {r["name"] for r in conn.execute("PRAGMA table_info(checkpoint)")}
        assert "activity_id" in ck


# ───────────────────────────────────────────────────────── activity registry


class TestActivities:
    def test_hours_are_summed_on_read_from_total_checkpoints(
        self, conn: sqlite3.Connection
    ) -> None:
        goal_id, _ = _seed_goal_with_target(conn)
        total_id = instantiate.add_total(conn, goal_id, "Shadowing hours", 60)
        aid = activities.add(
            conn, title="Cardiology shadowing", org="Banner Health", category="shadowing"
        )
        checkpoints.record(
            conn, total_id, source="manual", delta=4, activity_id=aid, note="Dr. R"
        )
        checkpoints.record(conn, total_id, source="manual", delta=3, activity_id=aid)
        checkpoints.record(conn, total_id, source="manual", delta=5)  # unattributed

        rows = activities.list_with_hours(conn)
        assert len(rows) == 1
        assert rows[0]["hours"] == 7
        assert rows[0]["entry_count"] == 2

    def test_cadence_checkpoints_never_count_as_hours(self, conn: sqlite3.Connection) -> None:
        _, cadence_id = _seed_goal_with_target(conn, kind="cadence")
        aid = activities.add(conn, title="Research", category="research")
        checkpoints.record(conn, cadence_id, source="manual", delta=3, activity_id=aid)
        assert activities.list_with_hours(conn)[0]["hours"] == 0

    def test_unknown_category_is_refused(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(activities.ActivityError):
            activities.add(conn, title="X", category="hobbies")

    def test_checkpoint_against_missing_activity_is_refused(
        self, conn: sqlite3.Connection
    ) -> None:
        goal_id, _ = _seed_goal_with_target(conn)
        total_id = instantiate.add_total(conn, goal_id, "Clinical hours", 150)
        with pytest.raises(checkpoints.CheckpointError):
            checkpoints.record(conn, total_id, source="manual", delta=1, activity_id=999)

    def test_entries_stream_carries_checkpoint_ids(self, conn: sqlite3.Connection) -> None:
        goal_id, _ = _seed_goal_with_target(conn)
        total_id = instantiate.add_total(conn, goal_id, "Shadowing hours", 60)
        aid = activities.add(conn, title="Shadowing", category="shadowing")
        recorded = checkpoints.record(
            conn, total_id, source="manual", delta=2, activity_id=aid, note="Dr. R · ICU"
        )
        entries = activities.entries_for(conn, aid)
        assert [e["id"] for e in entries] == [recorded.checkpoint_id]
        assert entries[0]["note"] == "Dr. R · ICU"


class TestLogHours:
    """One-line hour logging. The activity ledger is the part of a pre-med record that
    cannot be reconstructed later, and it stays empty as long as logging means opening
    a browser and filling a form."""

    def test_hours_land_on_the_accumulator_the_category_feeds(
        self, conn: sqlite3.Connection
    ) -> None:
        goal_id, _ = _seed_goal_with_target(conn)
        total_id = instantiate.add_total(
            conn, goal_id, "Shadowing hours (3+ specialties)", 60
        )
        aid = activities.add(conn, title="Cardiology", category="shadowing")

        logged = activities.log_hours(
            conn, activity_id=aid, hours=4, occurred_at="2026-08-02T18:00:00-07:00"
        )

        assert logged.target_id == total_id
        assert logged.hours == 4
        assert logged.target_done == 4
        assert logged.target_total == 60
        assert activities.list_with_hours(conn)[0]["hours"] == 4

    def test_the_title_is_prose_and_the_match_survives_it(
        self, conn: sqlite3.Connection
    ) -> None:
        # The real preset ships "Non-clinical service hours" for `volunteering` — a
        # title with neither the category word nor an obvious stem. If this breaks, the
        # hint table is wrong, not the caller.
        goal_id, _ = _seed_goal_with_target(conn)
        instantiate.add_total(conn, goal_id, "Non-clinical service hours", 500)
        aid = activities.add(conn, title="Food bank", category="volunteering")
        assert activities.log_hours(
            conn, activity_id=aid, hours=3, occurred_at="2026-08-02T12:00:00-07:00"
        ).hours == 3

    def test_clinical_does_not_steal_the_non_clinical_total(
        self, conn: sqlite3.Connection
    ) -> None:
        # "Non-clinical service hours" contains "clinical". Ordering by target id means
        # whichever was instantiated first wins a naive substring match, so the real
        # preset's order is reproduced here: clinical is added first and must still be
        # the one clinical hours land on, and volunteering must not land there.
        goal_id, _ = _seed_goal_with_target(conn)
        clinical_id = instantiate.add_total(
            conn, goal_id, "Clinical experience hours (paid or volunteer)", 500
        )
        service_id = instantiate.add_total(conn, goal_id, "Non-clinical service hours", 500)

        clinical = activities.add(conn, title="ED scribe", category="clinical")
        service = activities.add(conn, title="Food bank", category="volunteering")

        assert activities.log_hours(
            conn, activity_id=clinical, hours=2, occurred_at="2026-08-02T09:00:00-07:00"
        ).target_id == clinical_id
        assert activities.log_hours(
            conn, activity_id=service, hours=2, occurred_at="2026-08-02T09:00:00-07:00"
        ).target_id == service_id

    def test_a_category_with_no_accumulator_says_so_instead_of_guessing(
        self, conn: sqlite3.Connection
    ) -> None:
        # `other` exists for activities that belong in the AMCAS list under no hour
        # category. Inventing a target would put a meaningless number on the roadmap.
        _seed_goal_with_target(conn)
        aid = activities.add(conn, title="Marching band", category="other")
        with pytest.raises(activities.ActivityError, match="no lifetime hour target"):
            activities.log_hours(
                conn, activity_id=aid, hours=2, occurred_at="2026-08-02T09:00:00-07:00"
            )

    def test_a_category_whose_total_was_never_instantiated_is_refused(
        self, conn: sqlite3.Connection
    ) -> None:
        goal_id, _ = _seed_goal_with_target(conn)
        instantiate.add_total(conn, goal_id, "Shadowing hours", 60)
        aid = activities.add(conn, title="Ochem TA", category="leadership")
        with pytest.raises(activities.ActivityError, match="no lifetime hour target"):
            activities.log_hours(
                conn, activity_id=aid, hours=1, occurred_at="2026-08-02T09:00:00-07:00"
            )

    def test_zero_and_negative_hours_are_refused(self, conn: sqlite3.Connection) -> None:
        goal_id, _ = _seed_goal_with_target(conn)
        instantiate.add_total(conn, goal_id, "Research hours", 200)
        aid = activities.add(conn, title="Lab", category="research")
        for bad in (0, -3):
            with pytest.raises(activities.ActivityError, match="positive"):
                activities.log_hours(
                    conn, activity_id=aid, hours=bad,
                    occurred_at="2026-08-02T09:00:00-07:00",
                )

    def test_the_logged_instant_is_the_callers_not_the_clocks(
        self, conn: sqlite3.Connection
    ) -> None:
        # CLAUDE.md rule 4 one layer down: the caller knows which local day this belongs
        # to. A 22:00 Phoenix session must keep its own offset, not be restamped.
        goal_id, _ = _seed_goal_with_target(conn)
        instantiate.add_total(conn, goal_id, "Research hours", 200)
        aid = activities.add(conn, title="Lab", category="research")
        activities.log_hours(
            conn, activity_id=aid, hours=2, occurred_at="2026-08-01T22:00:00-07:00"
        )
        assert activities.entries_for(conn, aid)[0]["occurred_at"] == (
            "2026-08-01T22:00:00-07:00"
        )

    def test_find_by_name_prefers_an_exact_title_over_a_substring(
        self, conn: sqlite3.Connection
    ) -> None:
        activities.add(conn, title="Lab", org="Chen Lab", category="research")
        activities.add(conn, title="Lab assistant training", category="research")
        assert [a["title"] for a in activities.find_by_name(conn, "lab")] == ["Lab"]

    def test_find_by_name_returns_every_candidate_when_ambiguous(
        self, conn: sqlite3.Connection
    ) -> None:
        # Filing four years of hours under the wrong activity is not recoverable, so an
        # ambiguous name returns all of them for the caller to disambiguate.
        activities.add(conn, title="Chen Lab", category="research")
        activities.add(conn, title="Rivera Lab", category="research")
        assert len(activities.find_by_name(conn, "lab")) == 2
        assert activities.find_by_name(conn, "nothing here") == []

    def test_find_by_name_matches_the_org_too(self, conn: sqlite3.Connection) -> None:
        activities.add(conn, title="ED scribe", org="Banner Health", category="clinical")
        assert [a["title"] for a in activities.find_by_name(conn, "banner")] == ["ED scribe"]


# ──────────────────────────────────────────────────────────── anki connector


def _anki_db(tmp_path: Path) -> Path:
    path = tmp_path / "collection.anki2"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE col (crt INTEGER);"
        "CREATE TABLE cards (id INTEGER PRIMARY KEY, queue INTEGER, due INTEGER);"
        "CREATE TABLE revlog (id INTEGER PRIMARY KEY, time INTEGER);"
    )
    crt = int(datetime(2026, 1, 1, 7, 0, tzinfo=UTC).timestamp())
    db.execute("INSERT INTO col (crt) VALUES (?)", (crt,))
    today_offset = (datetime.now(tz=ZoneInfo(TZ)).date() - date(2026, 1, 1)).days
    db.execute("INSERT INTO cards (queue, due) VALUES (2, ?)", (today_offset,))
    db.execute("INSERT INTO cards (queue, due) VALUES (2, ?)", (today_offset - 3,))
    db.execute("INSERT INTO cards (queue, due) VALUES (2, ?)", (today_offset + 30,))
    db.execute("INSERT INTO cards (queue, due) VALUES (-1, ?)", (today_offset,))
    yesterday_noon = datetime.now(tz=ZoneInfo(TZ)).replace(
        hour=12, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)
    base = int(yesterday_noon.timestamp() * 1000)
    db.execute("INSERT INTO revlog (id, time) VALUES (?, ?)", (base, 30_000))
    db.execute("INSERT INTO revlog (id, time) VALUES (?, ?)", (base + 1000, 90_000))
    db.commit()
    db.close()
    return path


class TestAnkiConnector:
    def test_first_fetch_yields_day_tally_and_due_snapshot(self, tmp_path: Path) -> None:
        connector = AnkiConnector(db_path=_anki_db(tmp_path), tz=TZ)
        items = list(connector.fetch(None))
        tallies = [i for i in items if i.external_id.startswith("reviews:")]
        dues = [i for i in items if i.external_id.startswith("due:")]
        assert len(tallies) == 1 and len(dues) == 1
        tally = json.loads(tallies[0].raw_json or "{}")
        assert tally["reviews"] == 2 and tally["minutes"] == 2
        # due today (offset) + overdue (offset-3); future and suspended excluded
        assert json.loads(dues[0].raw_json or "{}")["due"] == 2

    def test_second_fetch_fetches_nothing(self, tmp_path: Path) -> None:
        connector = AnkiConnector(db_path=_anki_db(tmp_path), tz=TZ)
        list(connector.fetch(None))
        cursor = connector.cursor
        again = AnkiConnector(db_path=connector.db_path, tz=TZ)
        assert list(again.fetch(cursor)) == []
        assert again.cursor == cursor

    def test_unreadable_cursor_is_a_full_scan(self, tmp_path: Path) -> None:
        connector = AnkiConnector(db_path=_anki_db(tmp_path), tz=TZ)
        assert list(connector.fetch("not-json")) != []

    def test_missing_store_degrades_health(self, tmp_path: Path) -> None:
        health = AnkiConnector(db_path=tmp_path / "absent.anki2", tz=TZ).health()
        assert not health.ok and "not found" in (health.detail or "")

    def test_wal_only_rows_are_visible(self, tmp_path: Path) -> None:
        """The verifier's refutation of immutable=1: the real stores are WAL, and
        an immutable open misses rows living only in the -wal file. mode=ro must
        see them while the writer still holds the database open."""
        path = _anki_db(tmp_path)
        writer = sqlite3.connect(path)
        writer.execute("PRAGMA journal_mode=WAL")
        base = int(datetime.now(tz=UTC).timestamp() * 1000)
        writer.execute("INSERT INTO revlog (id, time) VALUES (?, ?)", (base, 5_000))
        writer.commit()  # committed, but sitting in the -wal, not the main file
        try:
            connector = AnkiConnector(db_path=path, tz=TZ)
            assert connector.health().ok
            items = list(connector.fetch(None))
            seen = sum(
                json.loads(i.raw_json or "{}").get("reviews", 0)
                for i in items
                if i.external_id.startswith("reviews:")
            )
            assert seen == 3  # the 2 baked-in reviews plus the WAL-only one
        finally:
            writer.close()

    def test_rescan_can_never_conflict_with_stored_batches(self, tmp_path: Path) -> None:
        """Same external_id must always mean same content (0002 trigger). A day
        ingested in two batches and then fully rescanned produces a third,
        differently-named item — never a rename of an old id with new numbers."""
        path = _anki_db(tmp_path)
        first = AnkiConnector(db_path=path, tz=TZ)
        emitted = {
            i.external_id: i.content_hash
            for i in first.fetch(None)
            if i.external_id.startswith("reviews:")
        }
        db = sqlite3.connect(path)
        newest = int(db.execute("SELECT MAX(id) AS m FROM revlog").fetchone()[0])
        db.execute("INSERT INTO revlog (id, time) VALUES (?, ?)", (newest + 500, 10_000))
        db.commit()
        db.close()
        second = AnkiConnector(db_path=path, tz=TZ)
        for item in second.fetch(first.cursor):
            if item.external_id.startswith("reviews:"):
                emitted[item.external_id] = item.content_hash
        rescan = AnkiConnector(db_path=path, tz=TZ)
        for item in rescan.fetch(None):  # cursor lost — full scan
            if item.external_id in emitted:
                assert item.content_hash == emitted[item.external_id]


# ─────────────────────────────────────────────────────────── avorio connector


def _avorio_db(tmp_path: Path) -> Path:
    path = tmp_path / "avorio.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE reviews (id TEXT PRIMARY KEY, card_id TEXT, rating TEXT, "
        " duration_ms INTEGER, reviewed_at TEXT);"
        "CREATE TABLE cards (id TEXT PRIMARY KEY, deck_id TEXT, due_date TEXT, "
        " card_state TEXT, suspended INTEGER DEFAULT 0, buried INTEGER DEFAULT 0);"
    )
    yesterday_utc = (datetime.now(tz=UTC) - timedelta(days=1)).replace(
        hour=19, minute=0, second=0, microsecond=0
    )
    stamp = yesterday_utc.strftime("%Y-%m-%d %H:%M:%S")
    later = (yesterday_utc + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "INSERT INTO reviews VALUES ('r1', 'c1', 'good', 20000, ?)", (stamp,)
    )
    db.execute(
        "INSERT INTO reviews VALUES ('r2', 'c1', 'again', 40000, ?)", (later,)
    )
    today = datetime.now(tz=ZoneInfo(TZ)).date().isoformat()
    db.execute(
        "INSERT INTO cards VALUES ('c1', 'd1', ?, 'review', 0, 0)", (today,)
    )
    db.execute("INSERT INTO cards VALUES ('c2', 'd1', '2030-01-01', 'review', 0, 0)")
    db.execute("INSERT INTO cards VALUES ('c3', 'd1', ?, 'new', 0, 0)", (today,))
    db.execute("INSERT INTO cards VALUES ('c4', 'd1', ?, 'review', 1, 0)", (today,))
    db.commit()
    db.close()
    return path


class TestAvorioConnector:
    def test_first_fetch_yields_day_tally_and_due_snapshot(self, tmp_path: Path) -> None:
        connector = AvorioConnector(db_path=_avorio_db(tmp_path), tz=TZ)
        items = list(connector.fetch(None))
        tallies = [i for i in items if i.external_id.startswith("reviews:")]
        dues = [i for i in items if i.external_id.startswith("due:")]
        assert len(tallies) == 1 and len(dues) == 1
        tally = json.loads(tallies[0].raw_json or "{}")
        assert tally["reviews"] == 2 and tally["minutes"] == 1
        # c1 due today; c2 future, c3 new, c4 suspended all excluded
        assert json.loads(dues[0].raw_json or "{}")["due"] == 1

    def test_second_fetch_fetches_nothing(self, tmp_path: Path) -> None:
        connector = AvorioConnector(db_path=_avorio_db(tmp_path), tz=TZ)
        list(connector.fetch(None))
        cursor = connector.cursor
        again = AvorioConnector(db_path=connector.db_path, tz=TZ)
        assert list(again.fetch(cursor)) == []

    def test_schema_drift_degrades_health_by_name(self, tmp_path: Path) -> None:
        path = tmp_path / "avorio.db"
        db = sqlite3.connect(path)
        db.executescript(
            "CREATE TABLE reviews (id TEXT, reviewed_at TEXT);"  # duration_ms gone
            "CREATE TABLE cards (id TEXT, due_date TEXT, card_state TEXT, "
            " suspended INTEGER, buried INTEGER);"
        )
        db.commit()
        db.close()
        health = AvorioConnector(db_path=path, tz=TZ).health()
        assert not health.ok
        assert "reviews.duration_ms" in (health.detail or "")


# ───────────────────────────────────────────────────── checkpoint wiring


class TestReviewCheckpoints:
    def test_review_days_become_checkpoints_once(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _, target_id = _seed_goal_with_target(conn)
        bound = settings.model_copy(update={"reviews_target_id": target_id})
        _review_day(conn, "anki", "2026-07-28", 120, 40)
        _review_day(conn, "avorio", "2026-07-29", 80, 25)

        assert reviews_mod.sync_checkpoints(conn, bound) == 2
        assert reviews_mod.sync_checkpoints(conn, bound) == 0  # idempotent

        rows = conn.execute(
            "SELECT source, source_item_id, delta FROM checkpoint WHERE target_id = ?",
            (target_id,),
        ).fetchall()
        assert len(rows) == 2
        assert all(r["source"] == "extraction" and r["source_item_id"] for r in rows)

    def test_second_batch_for_a_counted_day_is_zero_delta(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _, target_id = _seed_goal_with_target(conn)
        bound = settings.model_copy(update={"reviews_target_id": target_id})
        _review_day(conn, "anki", "2026-07-29", 50, 15)
        reviews_mod.sync_checkpoints(conn, bound)
        _seed_source_item(
            conn,
            source="anki",
            external_id="reviews:2026-07-29:evening",
            occurred_at="2026-07-29T23:00:00+00:00",
            raw={"date": "2026-07-29", "reviews": 30, "minutes": 10},
        )
        reviews_mod.sync_checkpoints(conn, bound)
        total = conn.execute(
            "SELECT SUM(delta) AS n FROM checkpoint WHERE target_id = ? "
            "AND date(occurred_at) = '2026-07-29'",
            (target_id,),
        ).fetchone()["n"]
        assert total == 1  # one day, counted once

    def test_unbound_config_writes_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _review_day(conn, "anki", "2026-07-28", 10, 5)
        assert reviews_mod.sync_checkpoints(conn, settings) == 0

    def test_a_manual_checkpoint_does_not_swallow_the_days_tally(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Verifier finding: only extraction checkpoints mark a day counted — an
        owner's manual tick on the same target that day is their action."""
        _, target_id = _seed_goal_with_target(conn)
        bound = settings.model_copy(update={"reviews_target_id": target_id})
        checkpoints.record(
            conn, target_id, source="manual", occurred_at="2026-07-29T09:00:00"
        )
        _review_day(conn, "anki", "2026-07-29", 50, 15)
        reviews_mod.sync_checkpoints(conn, bound)
        extracted = conn.execute(
            "SELECT delta FROM checkpoint WHERE target_id = ? AND source = 'extraction'",
            (target_id,),
        ).fetchone()
        assert extracted["delta"] == 1

    def test_streak_walks_back_and_today_is_grace(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _, target_id = _seed_goal_with_target(conn)
        bound = settings.model_copy(update={"reviews_target_id": target_id})
        for day in ("2026-07-27", "2026-07-28", "2026-07-29"):
            _review_day(conn, "anki", day, 10, 5)
        reviews_mod.sync_checkpoints(conn, bound)
        assert reviews_mod.streak(conn, bound, date(2026, 7, 30)) == 3
        assert reviews_mod.streak(conn, bound, date(2026, 7, 31)) == 0  # gap broke it


# ─────────────────────────────────────────── capacity, brief, page round-trip


class TestDownstream:
    def test_due_load_reduces_capacity_by_measured_pace(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        day = date(2026, 7, 30)
        before = capacity.compute(conn, settings, day, events=[])
        _review_day(conn, "anki", "2026-07-29", 100, 50)  # 30 s/card measured
        _due_day(conn, "anki", "2026-07-30", 60)
        after = capacity.compute(conn, settings, day, events=[])
        assert after.review_minutes == 30  # 60 cards × 30 s
        assert after.capacity_minutes == before.capacity_minutes - 30

    def test_no_snapshot_means_no_reduction(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        cap = capacity.compute(conn, settings, date(2026, 7, 30), events=[])
        assert cap.review_minutes == 0

    def test_idle_inflated_pace_is_clamped(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Verifier finding: the owner's real store had "8 reviews · 155 min" —
        wall-clock idle, not review time. Unclamped it zeroed the day."""
        _review_day(conn, "avorio", "2026-07-29", 8, 155)
        assert reviews_mod.trailing_seconds_per_card(conn, date(2026, 7, 30)) == 60.0

    def test_review_reservation_is_capped_not_day_zeroing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        day = date(2026, 7, 30)
        before = capacity.compute(conn, settings, day, events=[])
        _review_day(conn, "avorio", "2026-07-29", 8, 155)  # clamps to 60 s/card
        _due_day(conn, "avorio", "2026-07-30", 573)  # uncapped: 573 min
        after = capacity.compute(conn, settings, day, events=[])
        assert after.review_minutes == capacity.REVIEW_CAP_MINUTES
        assert after.capacity_minutes == before.capacity_minutes - 120
        assert after.capacity_minutes > 0  # the day survives
        # The brief still tells the truth about the full load.
        assert reviews_mod.review_minutes(conn, day)[0] == 573

    def test_brief_line_carries_due_count_and_provenance(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.brief.daily import goal_section

        _due_day(conn, "avorio", "2026-07-30", 140)
        section = goal_section(conn, date(2026, 7, 30), settings)
        review_lines = [ln for ln in section.lines if ln.text.startswith("Reviews:")]
        assert len(review_lines) == 1
        assert "140 due" in review_lines[0].text
        assert review_lines[0].provenance is not None

    def test_brief_says_nothing_on_a_quiet_day(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.brief.daily import goal_section

        section = goal_section(conn, date(2026, 7, 30), settings)
        assert not [ln for ln in section.lines if ln.text.startswith("Reviews:")]

    def test_log_with_activity_round_trips_on_the_roadmap_page(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
        client.post("/roadmaps/start/medical", follow_redirects=True)
        rid = int(conn.execute("SELECT id FROM roadmap").fetchone()["id"])
        tid = int(
            conn.execute(
                "SELECT t.id FROM target t JOIN roadmap r ON r.goal_id = t.goal_id "
                "WHERE r.id = ? AND t.kind = 'total' ORDER BY t.id LIMIT 1",
                (rid,),
            ).fetchone()["id"]
        )
        added = client.post(
            f"/roadmaps/{rid}/activities",
            data={"title": "ER scribe", "org": "Valleywise", "role": "Scribe",
                  "category": "clinical"},
        )
        assert added.status_code == 200 and "ER scribe" in added.text
        aid = int(conn.execute("SELECT id FROM activity").fetchone()["id"])
        logged = client.post(
            f"/roadmaps/{rid}/totals/{tid}/log",
            data={"amount": "6", "note": "night shift", "activity_id": str(aid)},
        )
        assert logged.status_code == 200
        assert "6 h" in logged.text and "across 1 entry" in logged.text
        row = conn.execute(
            "SELECT activity_id, delta FROM checkpoint WHERE target_id = ?", (tid,)
        ).fetchone()
        assert row["activity_id"] == aid and row["delta"] == 6

    def test_meaningful_toggle_and_slot_count_render(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
        client.post("/roadmaps/start/medical", follow_redirects=True)
        rid = int(conn.execute("SELECT id FROM roadmap").fetchone()["id"])
        client.post(
            f"/roadmaps/{rid}/activities", data={"title": "Clinic", "category": "clinical"}
        )
        aid = int(conn.execute("SELECT id FROM activity").fetchone()["id"])
        page = client.post(f"/roadmaps/{rid}/activities/{aid}/meaningful")
        assert "most meaningful" in page.text
        assert "1 of 15 slots" in page.text


# ─────────────────────────────────────────────── structured-source bypass


class _TallyConnector:
    """Minimal structured connector: one anki review-day tally, like the real one."""

    def __init__(self, source: str = "anki") -> None:
        self._source = source
        self.cursor = None

    @property
    def name(self) -> str:
        return self._source

    def health(self):  # type: ignore[no-untyped-def]
        from backglass.connectors.base import Health

        return Health(name=self._source, ok=True)

    def fetch(self, since):  # type: ignore[no-untyped-def]
        from backglass.connectors.base import SourceItem, content_hash

        raw = {"date": "2026-07-28", "reviews": 120, "minutes": 40}
        body = json.dumps(raw, sort_keys=True)
        yield SourceItem(
            source=self._source,
            external_id="reviews:2026-07-28:2026-07-28T20:00:00",
            occurred_at="2026-07-28T20:00:00+00:00",
            content_hash=content_hash(
                author=self._source,
                title="anki reviews · 2026-07-28",
                body_text=body,
                occurred_at="2026-07-28T20:00:00+00:00",
            ),
            author=self._source,
            title="anki reviews · 2026-07-28",
            body_text=body,
            raw_json=body,
        )


class TestStructuredSourceBypass:
    """Tier-0 drops structured tallies: zero model calls, downstream intact."""

    def test_tally_is_rule_dropped_and_checkpoints_still_land(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.sync import sync
        from tests.conftest import FakeModel

        _, target_id = _seed_goal_with_target(conn)
        bound = settings.model_copy(update={"reviews_target_id": target_id})
        model = FakeModel({})

        report = sync(conn, bound, [_TallyConnector()], model)

        assert report.rule_dropped == 1, "the tally must die in tier 0"
        assert report.model_triaged == 0
        assert model.calls == [], "no model call for a structured item, ever"
        reason = conn.execute(
            "SELECT triage_reason FROM source_item WHERE source = 'anki'"
        ).fetchone()["triage_reason"]
        assert "structured source" in reason
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM checkpoint WHERE target_id = ?", (target_id,)
        ).fetchone()["n"]
        assert n == 1, "reviews checkpoint wiring must survive the rule drop"

        second = sync(conn, bound, [_TallyConnector()], model)
        assert second.writes == 0
        assert model.calls == []


class TestLogCommand:
    """`backglass log` — the fast path. Every test drives the real CLI against a tmp
    database; none can reach the owner's ledger."""

    @pytest.fixture
    def cli_env(self, tmp_path, monkeypatch):
        from backglass import __main__ as cli
        from backglass.config import Settings
        from backglass.db import connect, migrate

        db = tmp_path / "backglass.db"
        conn = connect(db)
        migrate(conn)
        goal_id, _ = _seed_goal_with_target(conn)
        instantiate.add_total(conn, goal_id, "Research hours", 200)
        instantiate.add_total(conn, goal_id, "Shadowing hours", 60)
        conn.commit()
        conn.close()
        made = Settings(db_path=db, default_tz="America/Phoenix", tz_ranges=[])
        monkeypatch.setattr(cli, "get_settings", lambda: made)
        return cli, made

    def _run(self, cli, *args):
        from typer.testing import CliRunner

        return CliRunner().invoke(cli.app, ["log", *args])

    def test_new_creates_the_activity_and_logs_in_one_line(self, cli_env) -> None:
        cli, settings = cli_env
        result = self._run(cli, "Chen Lab", "3", "--new", "research", "--note", "blot")
        assert result.exit_code == 0, result.output
        assert "+3h Chen Lab" in result.output
        assert "Research hours 3/200" in result.output

        from backglass.db import connect

        conn = connect(settings.db_path)
        rows = activities.list_with_hours(conn)
        assert [(r["title"], r["hours"]) for r in rows] == [("Chen Lab", 3)]
        assert activities.entries_for(conn, int(rows[0]["id"]))[0]["note"] == "blot"
        conn.close()

    def test_a_second_log_accumulates_on_the_same_target(self, cli_env) -> None:
        cli, _ = cli_env
        self._run(cli, "Chen Lab", "3", "--new", "research")
        result = self._run(cli, "chen", "2")
        assert result.exit_code == 0, result.output
        assert "Research hours 5/200" in result.output

    def test_an_unknown_name_says_how_to_create_it(self, cli_env) -> None:
        cli, _ = cli_env
        result = self._run(cli, "Nowhere", "2")
        assert result.exit_code == 1
        assert "--new" in result.output

    def test_an_ambiguous_name_lists_the_candidates_and_writes_nothing(
        self, cli_env
    ) -> None:
        cli, settings = cli_env
        self._run(cli, "Chen Lab", "1", "--new", "research")
        self._run(cli, "Rivera Lab", "1", "--new", "research")

        result = self._run(cli, "lab", "5")

        assert result.exit_code == 1
        assert "matches 2 activities" in result.output
        from backglass.db import connect

        conn = connect(settings.db_path)
        assert [r["hours"] for r in activities.list_with_hours(conn)] == [1, 1]
        conn.close()

    def test_an_unknown_category_is_refused_before_anything_is_created(
        self, cli_env
    ) -> None:
        cli, settings = cli_env
        result = self._run(cli, "Marching band", "2", "--new", "hobbies")
        assert result.exit_code == 1
        assert "unknown category" in result.output
        from backglass.db import connect

        conn = connect(settings.db_path)
        assert activities.list_with_hours(conn) == []
        conn.close()

    def test_on_logs_against_the_named_day_not_today(self, cli_env) -> None:
        cli, settings = cli_env
        result = self._run(cli, "Chen Lab", "4", "--new", "research", "--on", "2026-07-14")
        assert result.exit_code == 0, result.output

        from backglass.db import connect

        conn = connect(settings.db_path)
        entry = activities.entries_for(conn, 1)[0]
        conn.close()
        # The owner's own offset, on the day they said — not the clock's, not UTC.
        assert str(entry["occurred_at"]).startswith("2026-07-14T")
        assert str(entry["occurred_at"]).endswith("-07:00")

    def test_a_category_with_no_accumulator_explains_itself(self, cli_env) -> None:
        cli, _ = cli_env
        result = self._run(cli, "Marching band", "2", "--new", "other")
        assert result.exit_code == 1
        assert "no lifetime hour target" in result.output


class TestLogSafety:
    """Second-round verifier findings. Each reproduces a scenario that reached the
    ledger, or would have."""

    @pytest.fixture
    def cli_env(self, tmp_path, monkeypatch):
        from backglass import __main__ as cli
        from backglass.config import Settings
        from backglass.db import connect, migrate

        db = tmp_path / "backglass.db"
        conn = connect(db)
        migrate(conn)
        goal_id, _ = _seed_goal_with_target(conn)
        instantiate.add_total(conn, goal_id, "Research hours", 200)
        conn.commit()
        conn.close()
        made = Settings(db_path=db, default_tz="America/Phoenix", tz_ranges=[])
        monkeypatch.setattr(cli, "get_settings", lambda: made)
        return cli, made

    def _run(self, cli, *args):
        from typer.testing import CliRunner

        return CliRunner().invoke(cli.app, ["log", *args])

    def _hours(self, settings):
        from backglass.db import connect

        conn = connect(settings.db_path)
        rows = activities.list_with_hours(conn)
        conn.close()
        return [(r["title"], r["hours"]) for r in rows]

    def test_an_absurd_hour_count_is_refused_before_it_can_overflow_the_ledger(
        self, cli_env
    ) -> None:
        # 2**63-1 is a legal SQLite INTEGER, so it committed — and every later
        # SUM(delta) then raised "integer overflow", taking out log, amcas-export and
        # both dashboard pages permanently, with no CLI path able to delete the row.
        cli, settings = cli_env
        self._run(cli, "Chen Lab", "1", "--new", "research")

        result = self._run(cli, "Chen Lab", str(2**63 - 1))

        assert result.exit_code == 1
        assert "not plausible" in result.output
        assert self._hours(settings) == [("Chen Lab", 1)]
        # And the ledger still reads afterwards, which is the whole point.
        assert self._run(cli, "Chen Lab", "2").exit_code == 0

    def test_a_value_too_large_for_sqlite_is_refused_without_a_traceback(
        self, cli_env
    ) -> None:
        """A value past SQLite's INTEGER range must be a refusal, not an OverflowError.

        The first version of this test asserted `"Traceback" not in result.output` and
        was vacuous: CliRunner puts the exception on `result.exception`, never in
        `.output`, so it passed against the unguarded code that printed a full traceback
        in a real terminal. The assertion has to name the exception itself.
        """
        cli, settings = cli_env
        self._run(cli, "Chen Lab", "1", "--new", "research")

        result = self._run(cli, "Chen Lab", "99999999999999999999")

        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit), result.exception
        assert "not plausible" in result.output
        assert self._hours(settings) == [("Chen Lab", 1)]

    def test_new_creates_nothing_when_the_category_has_no_target(self, cli_env) -> None:
        # The connection is autocommit, so `add` was durable before log_hours could
        # fail; the natural retry then made a second activity, and a third, until the
        # plain name was permanently ambiguous and the hours split across duplicates.
        cli, settings = cli_env
        result = self._run(cli, "Food bank", "2", "--new", "other")
        assert result.exit_code == 1
        assert "nothing was created" in result.output
        assert self._hours(settings) == []

    def test_new_refuses_to_duplicate_an_existing_activity(self, cli_env) -> None:
        cli, settings = cli_env
        self._run(cli, "Chen Lab", "1", "--new", "research")
        result = self._run(cli, "Chen Lab", "2", "--new", "research")
        assert result.exit_code == 1
        assert "already exists" in result.output
        assert self._hours(settings) == [("Chen Lab", 1)]

    def test_new_refuses_an_absurd_amount_before_creating_the_activity(
        self, cli_env
    ) -> None:
        cli, settings = cli_env
        result = self._run(cli, "Chen Lab", str(2**63 - 1), "--new", "research")
        assert result.exit_code == 1
        assert self._hours(settings) == []

    def test_a_malformed_on_date_is_refused_without_a_traceback(self, cli_env) -> None:
        cli, settings = cli_env
        self._run(cli, "Chen Lab", "1", "--new", "research")
        result = self._run(cli, "Chen Lab", "2", "--on", "not-a-date")
        assert result.exit_code == 1
        assert "not a date" in result.output
        assert "Traceback" not in result.output
        assert self._hours(settings) == [("Chen Lab", 1)]

    def test_a_future_date_is_refused(self, cli_env) -> None:
        # The accumulator has no date filter, so a future entry inflates every progress
        # bar immediately and stays wrong until the day arrives.
        cli, settings = cli_env
        self._run(cli, "Chen Lab", "1", "--new", "research")
        result = self._run(cli, "Chen Lab", "2", "--on", "9999-12-31")
        assert result.exit_code == 1
        assert "future" in result.output
        assert self._hours(settings) == [("Chen Lab", 1)]

    def test_a_bad_on_date_creates_no_activity(self, cli_env) -> None:
        """`--new` plus a rejected `--on` used to leave an orphan.

        `add` ran before `--on` was validated, and the connection is autocommit, so the
        activity was durable before the refusal. There is no rename or delete for an
        activity anywhere, so the orphan is permanent and consumes one of the fifteen
        AMCAS slots. Every refusal now resolves before the first write.
        """
        cli, settings = cli_env
        for bad in ("9999-12-31", "not-a-date", "0001-01-01"):
            result = self._run(cli, "Chen Lab", "4", "--new", "research", "--on", bad)
            assert result.exit_code == 1, bad
            assert "new activity" not in result.output, bad
            assert self._hours(settings) == [], bad

    def test_an_implausibly_old_date_is_refused(self, cli_env) -> None:
        # Pre-2000 dates land in local mean time (-07:28:18), an offset nothing else in
        # the ledger uses.
        cli, settings = cli_env
        self._run(cli, "Chen Lab", "1", "--new", "research")
        result = self._run(cli, "Chen Lab", "2", "--on", "0001-01-01")
        assert result.exit_code == 1
        assert self._hours(settings) == [("Chen Lab", 1)]


class TestCategoryResolutionCannotMisfile:
    """`total_target_for` matches title keywords, so the ways it can be wrong are the
    ways an owner might retitle a target or start a second goal."""

    def test_a_retitled_clinical_total_does_not_capture_volunteering(
        self, conn: sqlite3.Connection
    ) -> None:
        # "Clinical hours, paid or volunteer" has no parentheses to strip, so the
        # exclusion has to do the work.
        goal_id, _ = _seed_goal_with_target(conn)
        clinical = instantiate.add_total(
            conn, goal_id, "Clinical hours, paid or volunteer", 500
        )
        service = instantiate.add_total(conn, goal_id, "Non-clinical service hours", 500)
        assert activities.total_target_for(conn, "clinical")["id"] == clinical
        assert activities.total_target_for(conn, "volunteering")["id"] == service

    def test_a_target_on_an_archived_goal_is_not_eligible(
        self, conn: sqlite3.Connection
    ) -> None:
        # An abandoned path keeps its target rows, and a lower id would otherwise let it
        # outrank the live goal's accumulator purely by age.
        old_goal, _ = _seed_goal_with_target(conn)
        instantiate.add_total(conn, old_goal, "Research hours", 100)
        conn.execute("UPDATE goal SET status = 'archived' WHERE id = ?", (old_goal,))
        live_goal, _ = _seed_goal_with_target(conn)
        live = instantiate.add_total(conn, live_goal, "Research hours", 200)
        conn.commit()

        assert activities.total_target_for(conn, "research")["id"] == live

    def test_the_owners_real_preset_titles_all_resolve_to_the_right_total(
        self, conn: sqlite3.Connection
    ) -> None:
        # The exact titles specs/roadmaps/medical.md instantiates, in its own order.
        goal_id, _ = _seed_goal_with_target(conn)
        ids = {
            "shadowing": instantiate.add_total(
                conn, goal_id, "Shadowing hours (3+ specialties, >=1 primary care)", 75
            ),
            "clinical": instantiate.add_total(
                conn, goal_id, "Clinical experience hours (paid or volunteer)", 500
            ),
            "volunteering": instantiate.add_total(
                conn, goal_id, "Non-clinical service hours", 500
            ),
            "research": instantiate.add_total(conn, goal_id, "Research hours", 1000),
            "leadership": instantiate.add_total(
                conn, goal_id, "Leadership and teaching hours", 150
            ),
        }
        conn.commit()
        for category, target_id in ids.items():
            assert activities.total_target_for(conn, category)["id"] == target_id, category


class TestGuardsLiveAtTheFunnel:
    """Third-round findings. Each first-round fix was written at the CLI call site, so
    the dashboard's own write path — the one the docs point owners at — bypassed it.
    These assert the guard where BOTH doors go through."""

    def test_the_dashboard_cannot_log_an_overflowing_amount(
        self, conn: sqlite3.Connection
    ) -> None:
        # Reproduced end-to-end by the verifier: POST amount=2**63-1 returned 200, and
        # from then on every page that SUMs deltas — roadmap, goals, dashboard index —
        # raised "integer overflow", including the page whose button is the only way to
        # delete the row.
        goal_id, _ = _seed_goal_with_target(conn)
        total_id = instantiate.add_total(conn, goal_id, "Research hours", 200)
        with pytest.raises(checkpoints.CheckpointError, match="out of range"):
            checkpoints.record(conn, total_id, source="manual", delta=2**63 - 1)
        assert conn.execute("SELECT COUNT(*) AS n FROM checkpoint").fetchone()["n"] == 0

    def test_an_unlog_may_still_be_negative_but_not_unbounded(
        self, conn: sqlite3.Connection
    ) -> None:
        # The bound is symmetric because removing a logged entry writes a negative delta.
        goal_id, _ = _seed_goal_with_target(conn)
        total_id = instantiate.add_total(conn, goal_id, "Research hours", 200)
        checkpoints.record(conn, total_id, source="manual", delta=-4)
        with pytest.raises(checkpoints.CheckpointError, match="out of range"):
            checkpoints.record(conn, total_id, source="manual", delta=-(2**63 - 1))

    def test_a_plausible_amount_still_records(self, conn: sqlite3.Connection) -> None:
        # The mirror direction: the bound must not start refusing real entries.
        goal_id, _ = _seed_goal_with_target(conn)
        total_id = instantiate.add_total(conn, goal_id, "Research hours", 200)
        checkpoints.record(conn, total_id, source="manual", delta=checkpoints.MAX_DELTA)
        assert conn.execute("SELECT SUM(delta) AS n FROM checkpoint").fetchone()["n"] == (
            checkpoints.MAX_DELTA
        )

    def test_add_refuses_a_duplicate_title_whatever_the_caller(
        self, conn: sqlite3.Connection
    ) -> None:
        # The dashboard's activity_add calls straight through activities.add with no
        # check of its own, so three POSTs made three "Chen Lab" rows and the name became
        # permanently unloggable — `log` can only report the ambiguity, and no rename or
        # delete exists.
        activities.add(conn, title="Chen Lab", category="research")
        with pytest.raises(activities.ActivityError, match="already exists"):
            activities.add(conn, title="Chen Lab", category="research")
        with pytest.raises(activities.ActivityError, match="already exists"):
            activities.add(conn, title="  chen lab  ", category="clinical")
        assert len(activities.list_with_hours(conn)) == 1

    def test_a_distinct_title_that_merely_overlaps_is_allowed(
        self, conn: sqlite3.Connection
    ) -> None:
        # The guard is an exact title match, not find_by_name's substring-and-org search
        # — that version refused "Chen" because "Chen Lab Neuroscience" existed, and said
        # an activity named "Chen" already existed when none did.
        activities.add(conn, title="Chen Lab Neuroscience", org="Banner Health",
                       category="research")
        activities.add(conn, title="Chen", category="research")
        activities.add(conn, title="Banner", category="clinical")
        assert len(activities.list_with_hours(conn)) == 3

    def test_non_clinical_is_stripped_however_it_is_punctuated(
        self, conn: sqlite3.Connection
    ) -> None:
        # Target titles are editable from the dashboard, so the separator is whatever the
        # owner typed. An en dash or a double space used to defeat the phrase-strip and
        # turn a volunteering total into a refusal.
        goal_id, _ = _seed_goal_with_target(conn)
        for title in ("Non-clinical volunteering hours", "Non  clinical volunteering",
                      "Non–clinical volunteering", "non_clinical volunteering"):
            conn.execute("DELETE FROM target WHERE kind = 'total'")
            target_id = instantiate.add_total(conn, goal_id, title, 500)
            conn.commit()
            found = activities.total_target_for(conn, "volunteering")
            assert found is not None and found["id"] == target_id, title


class TestTheDashboardDoorIsGuarded:
    """The routes, not the functions. The claim that both doors are covered rested on a
    grep for INSERT sites; these exercise the doors themselves."""

    @pytest.fixture
    def web(self, tmp_path):
        from fastapi.testclient import TestClient

        from backglass.config import Settings
        from backglass.db import connect, migrate
        from backglass.web.app import create_app

        db = tmp_path / "b.db"
        conn = connect(db)
        migrate(conn)
        goal_id, _ = _seed_goal_with_target(conn)
        target_id = instantiate.add_total(conn, goal_id, "Research hours", 200)
        conn.execute(
            "INSERT INTO roadmap (user_id, path_id, path_version, title, goal_id, status,"
            " created_at) VALUES (1, 'medical', '2', 'R', ?, 'active',"
            " '2026-07-01T00:00:00Z')",
            (goal_id,),
        )
        roadmap_id = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
        conn.commit()
        conn.close()
        client = TestClient(
            create_app(Settings(db_path=db)),
            base_url="http://127.0.0.1:8765",
            raise_server_exceptions=False,
        )
        return client, roadmap_id, target_id, db

    def _activities(self, db):
        from backglass.db import connect

        conn = connect(db)
        rows = [str(r["title"]) for r in activities.list_with_hours(conn)]
        conn.close()
        return rows

    def test_the_roadmap_log_form_cannot_overflow_the_totals(self, web) -> None:
        client, roadmap_id, target_id, db = web
        response = client.post(
            f"/roadmaps/{roadmap_id}/totals/{target_id}/log",
            data={"amount": str(2**63 - 1), "note": "", "activity_id": "0"},
        )
        assert response.status_code == 422
        assert client.get(f"/roadmaps/{roadmap_id}").status_code == 200
        assert client.get("/").status_code == 200

    def test_the_goals_log_form_refuses_readably_rather_than_500ing(self, web) -> None:
        # Its sibling on the roadmap page always caught CheckpointError; this one did
        # not, so the owner saw "The server refused that (500)" instead of the reason.
        client, _roadmap_id, target_id, _db = web
        response = client.post(
            f"/goals/targets/{target_id}/log", data={"amount": "50000", "note": ""}
        )
        assert response.status_code == 422
        assert "separately" in response.text

    def test_the_dashboard_add_form_refuses_a_duplicate_activity(self, web) -> None:
        client, roadmap_id, _target_id, db = web
        form = {"title": "Chen Lab", "org": "", "role": "", "category": "research"}
        assert client.post(f"/roadmaps/{roadmap_id}/activities", data=form).status_code == 200
        assert client.post(f"/roadmaps/{roadmap_id}/activities", data=form).status_code == 422
        assert self._activities(db) == ["Chen Lab"]

    def test_an_accented_title_cannot_be_duplicated_by_changing_its_case(
        self, web
    ) -> None:
        """SQLite's LOWER() folds ASCII only, so the guard called these distinct while
        find_by_name — which decides *which* activity a name means — called them the
        same. Both rows got created, the name then matched two of them, and it could
        never be logged against again."""
        client, roadmap_id, _target_id, db = web
        base = {"org": "", "role": "", "category": "volunteering"}
        first = client.post(
            f"/roadmaps/{roadmap_id}/activities", data={**base, "title": "Café Latino"}
        )
        second = client.post(
            f"/roadmaps/{roadmap_id}/activities", data={**base, "title": "CAFÉ LATINO"}
        )

        assert first.status_code == 200
        assert second.status_code == 422
        assert self._activities(db) == ["Café Latino"]

    def test_the_name_stays_loggable_afterwards(self, web) -> None:
        # The harm was never the extra row itself — it was that the name became
        # ambiguous and therefore permanently unloggable, with no rename or delete.
        from backglass.db import connect

        client, roadmap_id, _target_id, db = web
        base = {"org": "", "role": "", "category": "volunteering"}
        client.post(f"/roadmaps/{roadmap_id}/activities", data={**base, "title": "Café Latino"})
        client.post(f"/roadmaps/{roadmap_id}/activities", data={**base, "title": "CAFÉ LATINO"})

        conn = connect(db)
        assert len(activities.find_by_name(conn, "Café Latino")) == 1
        assert len(activities.find_by_name(conn, "CAFÉ LATINO")) == 1
        conn.close()
