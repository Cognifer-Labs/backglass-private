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
