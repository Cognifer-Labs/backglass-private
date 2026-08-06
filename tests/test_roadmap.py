"""Roadmap engine: preset loading, instantiation, and the owner's adjustments.

The load-bearing assertions are the ones about the *goal engine reading roadmaps unchanged*
— a skipped step has to disappear from `targets.progress`, and a redated step has to move
`goals.health.risk`'s target date. If those two hold, the roadmap layer is genuinely just
rows the existing engine already understands.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from backglass.config import Settings
from backglass.goals import health, targets
from backglass.roadmap import presets
from backglass.roadmap.adjust import (
    AdjustError,
    complete_step,
    drop_roadmap,
    move_step,
    redate_step,
    set_cadence,
    skip_step,
    unskip_step,
)
from backglass.roadmap.instantiate import (
    AddedStep,
    Adjustments,
    CadenceCount,
    StepAction,
    instantiate,
)

START = date(2026, 1, 5)  # a Monday


def _make(
    conn: sqlite3.Connection,
    settings: Settings,
    path_id: str = "swe",
    adjustments: Adjustments | None = None,
) -> tuple[int, presets.Preset]:
    preset = presets.load(path_id)
    return instantiate(conn, settings, preset, START, adjustments), preset


def _step_id(conn: sqlite3.Connection, roadmap_id: int, step_key: str) -> int:
    row = conn.execute(
        "SELECT id FROM roadmap_step WHERE roadmap_id = ? AND step_key = ?",
        (roadmap_id, step_key),
    ).fetchone()
    assert row is not None, step_key
    return int(row["id"])


def _goal_target_date(conn: sqlite3.Connection, roadmap_id: int) -> str:
    row = conn.execute(
        "SELECT g.target_date FROM goal g JOIN roadmap r ON r.goal_id = g.id WHERE r.id = ?",
        (roadmap_id,),
    ).fetchone()
    return str(row["target_date"])


# ══ the loader ════════════════════════════════════════════════════════════


def test_all_presets_load() -> None:
    loaded = {p.id for p in presets.list_paths()}
    assert loaded == {"founder", "swe", "pm", "medical", "app-launch",
                      "ship-blocked-product", "company-revenue"}
    for preset in presets.list_paths():
        assert 5 <= len(preset.steps) <= 8
        assert preset.horizon in ("annual", "quarterly")
        assert preset.definition_of_done


def test_medical_spans_about_four_years() -> None:
    preset = presets.load("medical")
    assert max(s.offset_weeks for s in preset.steps) >= 200


def _write(tmp_path: Path, name: str, body: str) -> Path:
    (tmp_path / f"{name}.md").write_text(body)
    return tmp_path


GOOD_FRONT = (
    "---\nid: t\nversion: 1\ntitle: T\nhorizon: annual\ndefinition_of_done: done\n---\n\n"
)


def test_duplicate_step_keys_are_rejected(tmp_path: Path) -> None:
    base = _write(
        tmp_path,
        "t",
        GOOD_FRONT
        + '## Steps\n\n```json\n{"steps": ['
        + '{"key": "a", "title": "A", "offset_weeks": 0},'
        + '{"key": "a", "title": "A again", "offset_weeks": 4}], "cadences": []}\n```\n',
    )
    with pytest.raises(presets.PresetError, match="duplicate step key"):
        presets.load("t", base)


def test_missing_frontmatter_key_is_rejected(tmp_path: Path) -> None:
    base = _write(
        tmp_path,
        "t",
        "---\nid: t\nversion: 1\ntitle: T\nhorizon: annual\n---\n\n"
        + '## Steps\n\n```json\n{"steps": '
        + '[{"key": "a", "title": "A", "offset_weeks": 0}], "cadences": []}\n```\n',
    )
    with pytest.raises(presets.PresetError, match="definition_of_done"):
        presets.load("t", base)


def test_step_referencing_unknown_cadence_is_rejected(tmp_path: Path) -> None:
    base = _write(
        tmp_path,
        "t",
        GOOD_FRONT
        + '## Steps\n\n```json\n{"steps": ['
        + '{"key": "a", "title": "A", "offset_weeks": 0, "cadences": ["nope"]}],'
        + ' "cadences": []}\n```\n',
    )
    with pytest.raises(presets.PresetError, match="unknown cadence"):
        presets.load("t", base)


def test_a_path_id_cannot_escape_the_presets_directory(tmp_path: Path) -> None:
    """`path_id` is a URL segment (/roadmaps/start/{path_id}), so the join is a traversal
    primitive: the parser reads the file and then quotes it back in its own errors."""
    base = tmp_path / "presets"
    base.mkdir()
    (base.parent / "secret.md").write_text("---\nid: secret\n---\n")

    for hostile in ("../secret", "../../etc/passwd", "sub/../../secret"):
        with pytest.raises(presets.PresetError) as caught:
            presets.load(hostile, base)
        assert str(base) not in str(caught.value), "the message maps the filesystem"


def test_a_missing_preset_is_named_by_id_not_by_absolute_path(tmp_path: Path) -> None:
    """A 422 body reaches the browser. It says what the caller asked for and nothing
    about where this machine keeps its files."""
    with pytest.raises(presets.PresetError) as caught:
        presets.load("nope", tmp_path)
    assert "'nope'" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


# ══ instantiation ═════════════════════════════════════════════════════════


def test_instantiation_creates_the_goal_targets_and_rows(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    roadmap_id, preset = _make(conn, settings)

    goals = conn.execute("SELECT * FROM goal").fetchall()
    assert len(goals) == 1
    assert goals[0]["title"] == preset.title
    assert goals[0]["horizon"] == preset.horizon
    assert goals[0]["definition_of_done"] == preset.definition_of_done

    kinds = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM target GROUP BY kind ORDER BY kind"
    ).fetchall()
    assert {row["kind"]: row["n"] for row in kinds} == {
        "cadence": len(preset.cadences),
        "milestone": len(preset.steps),
    }

    steps = conn.execute(
        "SELECT * FROM roadmap_step WHERE roadmap_id = ? ORDER BY sort_order", (roadmap_id,)
    ).fetchall()
    assert [s["step_key"] for s in steps] == [s.key for s in preset.steps]
    assert [s["sort_order"] for s in steps] == [i * 10 for i in range(len(preset.steps))]
    assert all(s["origin"] == "preset" for s in steps)
    assert all(s["target_id"] is not None for s in steps)

    cadences = conn.execute(
        "SELECT * FROM roadmap_cadence WHERE roadmap_id = ?", (roadmap_id,)
    ).fetchall()
    assert {c["cadence_key"] for c in cadences} == {c.key for c in preset.cadences}


def test_path_version_is_stamped(conn: sqlite3.Connection, settings: Settings) -> None:
    roadmap_id, preset = _make(conn, settings)
    row = conn.execute("SELECT * FROM roadmap WHERE id = ?", (roadmap_id,)).fetchone()
    assert (row["path_id"], row["path_version"]) == (preset.id, preset.version)
    assert row["status"] == "active"


def test_goal_target_date_is_the_last_step(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    roadmap_id, preset = _make(conn, settings)
    latest = START + timedelta(weeks=max(s.offset_weeks for s in preset.steps))
    assert _goal_target_date(conn, roadmap_id) == latest.isoformat()


def test_planned_dates_are_offset_weeks_from_the_start(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    roadmap_id, preset = _make(conn, settings)
    first = preset.steps[0]
    row = conn.execute(
        "SELECT planned_date FROM roadmap_step WHERE roadmap_id = ? AND step_key = ?",
        (roadmap_id, first.key),
    ).fetchone()
    expected = START + timedelta(weeks=first.offset_weeks)
    assert row["planned_date"] == expected.isoformat()


# ══ step completion ═══════════════════════════════════════════════════════


def test_completing_a_step_twice_records_one_checkpoint(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    roadmap_id, _ = _make(conn, settings)
    step_id = _step_id(conn, roadmap_id, "resume")

    first = complete_step(conn, step_id, date(2026, 1, 9))
    second = complete_step(conn, step_id, date(2026, 2, 9))

    assert first.status == second.status == "done"
    assert second.checkpoint_id is None  # the second call wrote nothing
    assert second.done_at == first.done_at
    count = conn.execute("SELECT COUNT(*) AS n FROM checkpoint").fetchone()["n"]
    assert count == 1


# ══ skip, redate, move, drop ══════════════════════════════════════════════


def test_skipping_a_step_removes_it_from_the_goal_engine(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    roadmap_id, preset = _make(conn, settings)
    step_id = _step_id(conn, roadmap_id, "offers")

    before = targets.progress(conn, settings, START)
    assert len(before) == len(preset.steps) + len(preset.cadences)

    skip_step(conn, step_id)

    row = conn.execute("SELECT * FROM roadmap_step WHERE id = ?", (step_id,)).fetchone()
    assert row["status"] == "skipped"
    assert conn.execute(
        "SELECT active FROM target WHERE id = ?", (row["target_id"],)
    ).fetchone()["active"] == 0

    after = targets.progress(conn, settings, START)
    assert len(after) == len(before) - 1
    assert "Offer in hand and negotiated" not in [t.title for t in after]

    # The last live step is now the target date, so risk projects against that instead.
    risks = health.risk(conn, settings, START)
    assert len(risks) == 1
    assert risks[0].target_date is not None
    assert risks[0].target_date.isoformat() == _goal_target_date(conn, roadmap_id)

    unskip_step(conn, step_id)
    assert len(targets.progress(conn, settings, START)) == len(before)
    with pytest.raises(AdjustError, match="not skipped"):
        unskip_step(conn, step_id)


def test_redating_the_last_step_moves_the_goal_target_date(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    roadmap_id, _ = _make(conn, settings)
    before = _goal_target_date(conn, roadmap_id)

    redate_step(conn, _step_id(conn, roadmap_id, "offers"), date(2027, 3, 1))

    after = _goal_target_date(conn, roadmap_id)
    assert after == "2027-03-01"
    assert after != before


def test_move_step_swaps_order(conn: sqlite3.Connection, settings: Settings) -> None:
    roadmap_id, preset = _make(conn, settings)
    keys = [s.key for s in preset.steps]
    move_step(conn, _step_id(conn, roadmap_id, keys[1]), "up")

    order = [
        row["step_key"]
        for row in conn.execute(
            "SELECT step_key FROM roadmap_step WHERE roadmap_id = ? ORDER BY sort_order",
            (roadmap_id,),
        )
    ]
    assert order[:2] == [keys[1], keys[0]]

    with pytest.raises(AdjustError, match="already at the up end"):
        move_step(conn, _step_id(conn, roadmap_id, keys[1]), "up")


def test_set_cadence_and_unknown_ids_raise(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    roadmap_id, _ = _make(conn, settings)
    set_cadence(conn, roadmap_id, "applications", 4)
    row = conn.execute(
        "SELECT t.weekly_count FROM target t JOIN roadmap_cadence rc ON rc.target_id = t.id "
        "WHERE rc.roadmap_id = ? AND rc.cadence_key = 'applications'",
        (roadmap_id,),
    ).fetchone()
    assert row["weekly_count"] == 4

    with pytest.raises(AdjustError):
        set_cadence(conn, roadmap_id, "nope", 1)
    with pytest.raises(AdjustError):
        complete_step(conn, 9999)


def test_drop_roadmap_drops_the_goal_too(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    roadmap_id, _ = _make(conn, settings)
    drop_roadmap(conn, roadmap_id)
    row = conn.execute(
        "SELECT r.status AS r_status, r.closed_at, g.status AS g_status "
        "FROM roadmap r JOIN goal g ON g.id = r.goal_id WHERE r.id = ?",
        (roadmap_id,),
    ).fetchone()
    assert row["r_status"] == "dropped"
    assert row["g_status"] == "dropped"
    assert row["closed_at"]
    assert targets.progress(conn, settings, START) == []


# ══ adjustments applied at instantiation ══════════════════════════════════


def test_adjustments_are_applied_and_unknown_keys_reported(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    adjustments = Adjustments(
        step_actions=[
            StepAction("projects", "skip"),
            StepAction("offers", "redate", date(2027, 6, 1)),
            StepAction("does_not_exist", "skip"),
        ],
        added_steps=[
            AddedStep("referrals", "Ask six people for referrals", date(2026, 3, 2), "resume")
        ],
        cadence_counts=[CadenceCount("applications", 3), CadenceCount("nope", 9)],
        summary="Employed full time, so fewer applications and a referral push first.",
    )
    roadmap_id, preset = _make(conn, settings, adjustments=adjustments)

    rows = conn.execute(
        "SELECT * FROM roadmap_step WHERE roadmap_id = ? ORDER BY sort_order", (roadmap_id,)
    ).fetchall()
    order = [row["step_key"] for row in rows]
    assert order[:2] == ["resume", "interview:referrals"]
    assert len(order) == len(preset.steps) + 1

    added = next(row for row in rows if row["step_key"] == "interview:referrals")
    assert added["origin"] == "interview"
    assert added["planned_date"] == "2026-03-02"
    assert added["target_id"] is not None

    skipped = next(row for row in rows if row["step_key"] == "projects")
    assert skipped["status"] == "skipped"
    assert conn.execute(
        "SELECT active FROM target WHERE id = ?", (skipped["target_id"],)
    ).fetchone()["active"] == 0

    assert next(r for r in rows if r["step_key"] == "offers")["planned_date"] == "2027-06-01"
    assert _goal_target_date(conn, roadmap_id) == "2027-06-01"

    applications = conn.execute(
        "SELECT t.weekly_count FROM target t JOIN roadmap_cadence rc ON rc.target_id = t.id "
        "WHERE rc.roadmap_id = ? AND rc.cadence_key = 'applications'",
        (roadmap_id,),
    ).fetchone()
    assert applications["weekly_count"] == 3

    assert conn.execute(
        "SELECT personalized FROM roadmap WHERE id = ?", (roadmap_id,)
    ).fetchone()["personalized"] == 1


def test_unknown_adjustment_keys_are_reported_not_raised(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    from backglass.roadmap.instantiate import apply_adjustments

    roadmap_id, _ = _make(conn, settings)
    notes = apply_adjustments(
        conn,
        roadmap_id,
        Adjustments(
            step_actions=[StepAction("ghost", "skip"), StepAction("resume", "sideways")],
            added_steps=[AddedStep("extra", "Extra", date(2026, 5, 4), "ghost")],
            cadence_counts=[CadenceCount("ghost_cadence", 2)],
        ),
    )

    assert any("unknown step key 'ghost'" in note for note in notes)
    assert any("unknown step action" in note for note in notes)
    assert any("unknown cadence key" in note for note in notes)
    assert any("appended" in note for note in notes)

    # The step with the bad anchor still landed, at the end.
    order = [
        row["step_key"]
        for row in conn.execute(
            "SELECT step_key FROM roadmap_step WHERE roadmap_id = ? ORDER BY sort_order",
            (roadmap_id,),
        )
    ]
    assert order[-1] == "interview:extra"
