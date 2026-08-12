"""Ground truth about this installation, with the derivation of every claim.

Written 2026-08-07 after a session that lost time to four separate flavours of the same
mistake: a screenshot that was a stale capture, a CSS scale remembered rather than read,
a cost premise carried from a measurement taken elsewhere, and a plan section silently
dropped by someone else's merge. None of them failed loudly. Each was believed until
something incidental contradicted it.

`status` answers "is it healthy" in prose for a person. This answers "what IS it" in a
shape a program can check, and it holds itself to three rules:

1. **Every claim names how it was derived.** A number with no derivation is a number you
   have to trust; one that names its query is one you can re-run. `how` is the point of
   this module, not decoration.
2. **Unknown is a value.** A probe that cannot run reports `unknown` with the reason. The
   failure this exists to prevent is a confident answer assembled from a missing input,
   so silence is never allowed to read as zero.
3. **Nothing here is cached or remembered.** Every field is read at call time from the
   database, the filesystem or git. This module has no state of its own on purpose.

The deployed-versus-source comparison is the one that motivated it. The desktop app
freezes templates and CSS into a PyInstaller bundle, so the running app can be arbitrarily
far behind the repo with nothing on either side saying so — and the only way that was ever
noticed was by rebuilding and looking. Here it is a file hash.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backglass.config import REPO_ROOT, Settings
from backglass.ledger import USER_ID

#: Where a rebuilt bundle lands. Absent on any machine that has not installed the app,
#: which is a legitimate state and reports as such rather than as a failure.
INSTALLED_APP = Path("/Applications/Backglass.app")

#: The stylesheets the sidecar freezes. If these differ, the running app renders
#: different markup from the checkout, whatever the version numbers say.
FROZEN_STYLESHEETS = ("backglass/web/static/dashboard.css", "design/tokens.css")

#: The templates are frozen too, and enumerating them by hand is what this check is
#: for — a list nobody remembers to extend reports "matches_source: True" about a
#: surface it never looked at.
FROZEN_TEMPLATE_DIR = "backglass/web/templates"

#: The scripts are frozen too, and until 2026-08-11 nothing compared them. That is the
#: gap that hid the Drop bug for as long as it hid: the behaviour of every button on the
#: dashboard lives in these files, `state` reported `matches_source: True` about an app
#: whose scripts it had never hashed, and a stale one would have looked identical to a
#: correct one. Globbed for the same reason the templates are — a script added later is
#: covered without anyone deciding to cover it.
FROZEN_SCRIPT_DIR = "backglass/web/static"


def frozen_surfaces() -> tuple[str, ...]:
    """Every repo-relative path the sidecar bundles, found rather than remembered.

    On 2026-08-09 the day timeline was fixed, the app kept drawing the old one, and
    `state` named `dashboard.css` alone — the two templates in the same change were
    equally stale and equally frozen, and nothing said so. A hardcoded tuple is a claim
    that someone updated it; globbing the template directory is a claim the filesystem
    can keep. A template added tomorrow is covered without anyone deciding to cover it.

    Sorted so the reported order is stable between runs, because a list that reorders
    reads like a change.
    """
    templates = sorted(
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / FROZEN_TEMPLATE_DIR).glob("*.html")
    )
    scripts = sorted(
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / FROZEN_SCRIPT_DIR).glob("*.js")
    )
    return (*FROZEN_STYLESHEETS, *templates, *scripts)


@dataclass
class Claim:
    """One fact, and the thing that would let you check it yourself."""

    value: Any
    how: str
    #: Set when the probe could not run. The value is then meaningless and readers must
    #: say so rather than print it.
    unknown: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"value": self.value, "how": self.how}
        if self.unknown:
            out["unknown"] = self.unknown
        return out


@dataclass
class State:
    sections: dict[str, dict[str, Claim]] = field(default_factory=dict)

    def add(self, section: str, name: str, claim: Claim) -> None:
        self.sections.setdefault(section, {})[name] = claim

    def as_dict(self) -> dict[str, Any]:
        return {
            section: {name: claim.as_dict() for name, claim in claims.items()}
            for section, claims in self.sections.items()
        }


def _git(*args: str) -> str | None:
    if shutil.which("git") is None:
        return None
    try:
        done = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _code(state: State) -> None:
    head = _git("rev-parse", "--short", "HEAD")
    state.add("code", "head", Claim(head, "git rev-parse --short HEAD",
                                    None if head else "git unavailable or not a repo"))
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    state.add("code", "branch", Claim(branch, "git rev-parse --abbrev-ref HEAD",
                                      None if branch else "git unavailable"))
    dirty = _git("status", "--porcelain")
    if dirty is None:
        state.add("code", "uncommitted", Claim(None, "git status --porcelain",
                                               "git unavailable"))
    else:
        # Porcelain is "XY path", but a rename is "XY old -> new" and a quoted path
        # carries spaces. Split off the two status columns and take the rest whole
        # rather than slicing a fixed offset, which silently ate a leading character.
        paths = [line[2:].strip() for line in dirty.splitlines() if line.strip()]
        state.add("code", "uncommitted", Claim(paths, "git status --porcelain"))
    # Unpushed work is the shape that strands a session's output. Counted, not listed.
    ahead = _git("rev-list", "--count", "@{upstream}..HEAD")
    state.add(
        "code", "commits_ahead_of_upstream",
        Claim(int(ahead) if ahead and ahead.isdigit() else None,
              "git rev-list --count @{upstream}..HEAD",
              None if ahead and ahead.isdigit() else "no upstream configured"),
    )


def _deployed(state: State) -> None:
    """Does the installed app render what this checkout says?

    Compared by hashing the frozen copies against the repo's, because that is the
    question — not which build ran last, but whether the bytes the app serves are the
    bytes here. A version number would have to be maintained; a hash cannot drift.
    """
    if not INSTALLED_APP.exists():
        state.add("deployed", "app", Claim(None, str(INSTALLED_APP), "not installed"))
        return
    state.add("deployed", "app", Claim(str(INSTALLED_APP), "path exists"))
    frozen_root = INSTALLED_APP / "Contents/Resources/sidecar/backglass-server/_internal"
    surfaces = frozen_surfaces()
    stale: list[str] = []
    missing: list[str] = []
    for relative in surfaces:
        # tokens.css is frozen under its repo-relative path inside _internal.
        theirs = _sha256(frozen_root / relative)
        ours = _sha256(REPO_ROOT / relative)
        if theirs is None or ours is None:
            missing.append(relative)
        elif theirs != ours:
            stale.append(relative)
    state.add(
        "deployed", "matches_source",
        Claim(not stale and not missing,
              f"sha256 of {len(surfaces)} frozen surfaces vs the checkout",
              "; ".join(f"{p} not found in the bundle" for p in missing) or None),
    )
    state.add("deployed", "stale_surfaces", Claim(stale, "sha256 mismatch vs the checkout"))


def _schema(conn: sqlite3.Connection, state: State) -> None:
    applied = [
        str(row["version"])
        for row in conn.execute("SELECT version FROM schema_version ORDER BY version")
    ]
    on_disk = sorted(p.name.split("_")[0] for p in
                     (REPO_ROOT / "backglass/db/migrations").glob("[0-9]*.sql"))
    state.add("schema", "applied", Claim(len(applied), "SELECT version FROM schema_version"))
    state.add("schema", "on_disk", Claim(len(on_disk), "backglass/db/migrations/*.sql"))
    # The trap this names: a frozen sidecar older than the database 500s on every page.
    pending = [v for v in on_disk if v.lstrip("0") not in {a.lstrip("0") for a in applied}]
    state.add("schema", "unapplied", Claim(pending, "migrations on disk with no row"))


def _prompts(conn: sqlite3.Connection, state: State) -> None:
    from backglass.extract import prompts as prompt_mod

    stamps: dict[str, str] = {}
    for path in sorted(prompt_mod.PROMPTS_DIR.glob("*.md")):
        try:
            stamps[path.stem] = prompt_mod.load(path.stem).stamp
        except Exception as exc:  # noqa: BLE001 - a malformed prompt is itself the news
            stamps[path.stem] = f"unreadable: {exc}"
    state.add("prompts", "on_disk",
              Claim(stamps, "frontmatter of specs/extraction-prompts/*.md"))
    ledger = [
        row["extraction_version"]
        for row in conn.execute(
            "SELECT DISTINCT extraction_version FROM source_item"
            " WHERE user_id = ? AND extraction_version IS NOT NULL", (USER_ID,)
        )
    ]
    # Rows extracted under an older stamp are pending re-extraction by design; saying so
    # here stops it reading as a backlog nobody noticed.
    state.add("prompts", "versions_in_the_ledger", Claim(sorted(ledger),
              "SELECT DISTINCT extraction_version FROM source_item"))


def _ledger(conn: sqlite3.Connection, settings: Settings, state: State) -> None:
    def count(sql: str, **params: Any) -> int:
        # Named, not positional: this connection's row factory keys by column name and
        # `row[0]` raises KeyError, which is how the first version of this probe failed
        # while every other section printed happily around it.
        row = conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    state.add("ledger", "source_items",
              Claim(count("SELECT COUNT(*) AS n FROM source_item WHERE user_id = :u",
                          u=USER_ID),
                    "SELECT COUNT(*) FROM source_item"))
    state.add("ledger", "open_commitments",
              Claim(count("SELECT COUNT(*) AS n FROM commitment WHERE user_id = :u"
                          " AND status = 'open'", u=USER_ID),
                    "SELECT COUNT(*) FROM commitment WHERE status = 'open'"))
    state.add("ledger", "untriaged",
              Claim(count("SELECT COUNT(*) AS n FROM source_item WHERE user_id = :u"
                          " AND triage_verdict IS NULL", u=USER_ID),
                    "source_item WHERE triage_verdict IS NULL"))
    state.add("ledger", "kept_not_extracted",
              Claim(count("SELECT COUNT(*) AS n FROM source_item WHERE user_id = :u"
                          " AND triage_verdict = 'keep' AND extraction_version IS NULL",
                          u=USER_ID),
                    "kept items with no extraction_version — pending or parked"))
    del settings


def _pipeline(conn: sqlite3.Connection, state: State) -> None:
    last = conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone()
    if last is None:
        state.add("pipeline", "last_run", Claim(None, "SELECT * FROM run ORDER BY id DESC",
                                                "no run has ever completed"))
    else:
        state.add("pipeline", "last_run",
                  Claim({"id": last["id"], "started_at": last["started_at"],
                         "degraded": bool(last["degraded"])},
                        "newest row in `run`"))
    calls = conn.execute(
        "SELECT tier, COUNT(*) n, ROUND(AVG(duration_ms)) ms, ROUND(SUM(cost_usd) * 100, 2) c"
        " FROM model_call WHERE user_id = ? GROUP BY tier", (USER_ID,)
    ).fetchall()
    state.add(
        "pipeline", "model_calls",
        Claim({r["tier"]: {"calls": r["n"], "mean_ms": r["ms"], "cents": r["c"]}
               for r in calls},
              "GROUP BY tier over model_call",
              "model_call is empty; it fills from the first sync after Phase 0"
              if not calls else None),
    )


def _knowledge_base(conn: sqlite3.Connection, settings: Settings, state: State) -> None:
    from backglass import facts as facts_mod

    active = facts_mod.recall(conn)
    state.add("knowledge_base", "facts", Claim(len(active), "facts.recall(conn)"))
    sourced = sum(
        1 for row in conn.execute(
            "SELECT source_item_id FROM fact WHERE user_id = ? AND superseded_by IS NULL"
            " AND status = 'active'", (USER_ID,))
        if row["source_item_id"] is not None
    )
    state.add("knowledge_base", "with_provenance",
              Claim(sourced, "active facts whose source_item_id is not NULL"))
    state.add("knowledge_base", "config_drift",
              Claim(facts_mod.config_drift(conn, settings), "facts.config_drift"))
    context = facts_mod.owner_context(conn)
    state.add("knowledge_base", "owner_context_chars",
              Claim(len(context), "len(facts.owner_context(conn)) — what triage now carries"))


def _open_questions(conn: sqlite3.Connection, state: State) -> None:
    """What the system knows it does not know.

    `state` is the answer to "what is true about this installation", and until now it
    only reported what Backglass believes. A conflict it cannot resolve, an hour it
    cannot name, two entities it suspects are one person — those are equally facts about
    the installation, and the most useful kind, because each is a thing the owner can
    settle in a sentence.

    Counted by kind rather than listed: the questions themselves have a surface, and a
    ground-truth report should say what is outstanding without becoming that surface.
    """
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM open_question"
        " WHERE user_id = ? AND status = 'open' GROUP BY kind ORDER BY kind",
        (USER_ID,),
    ).fetchall()
    by_kind = {str(row["kind"]): int(row["n"]) for row in rows}
    state.add(
        "open_questions", "waiting",
        Claim(sum(by_kind.values()), "open_question WHERE status = 'open'"),
    )
    state.add("open_questions", "by_kind", Claim(by_kind, "GROUP BY kind over the same rows"))
    answered = conn.execute(
        "SELECT COUNT(*) AS n FROM open_question WHERE user_id = ? AND status = 'answered'",
        (USER_ID,),
    ).fetchone()
    state.add(
        "open_questions", "answered",
        Claim(int(answered["n"]), "status = 'answered' — each also recorded as a decision"),
    )


def _retrieval(conn: sqlite3.Connection, settings: Settings, state: State) -> None:
    """How much of the corpus a search can actually reach.

    The number that makes the feature honest. A search over a fifth of the ledger returns
    results that look exactly like a search over all of it, and the difference only shows
    when the answer was in the part that was not indexed — so the proportion belongs in
    the ground-truth report rather than being inferrable from results that came back fine.

    Zero indexed is a legitimate state, not a fault: retrieval is additive, and an
    installation that never runs `search index` is fully correct without it.
    """
    from backglass import search as search_mod

    stats = search_mod.coverage(conn, settings)
    state.add(
        "retrieval", "indexed",
        Claim(f"{stats['indexed']} of {stats['indexable']}",
              "COUNT over embedding vs kept source_item with body text"),
    )
    state.add("retrieval", "model", Claim(stats["model"], "settings.embedding_model"))
    state.add(
        "retrieval", "pending",
        Claim(stats["pending"], "indexable minus indexed — documents a search cannot reach"),
    )


#: Where `schedule install` puts the jobs, and the prefix that marks them as ours.
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
JOB_PREFIX = "com.backglass."

#: How far a job's last observed fire may sit from its scheduled time before this
#: reports it as drifting.
#:
#: Generous on purpose. launchd defers a `StartCalendarInterval` job that came due while
#: the machine was asleep and runs it at wake, so a late fire is ordinary and not news.
#: What this is looking for is the failure below, which was hours wide and constant.
SCHEDULE_DRIFT_TOLERANCE_MINUTES = 45


def _last_fire(plist: dict[str, Any]) -> float | None:
    """When the job last actually ran, from the mtime of the streams it writes to.

    launchd keeps no accessible record of the last fire — `launchctl print` reports how
    many times a job has run since it was loaded, and not once when. The log is the only
    durable evidence, and every one of these jobs writes to one on every run.
    """
    stamps = [
        path.stat().st_mtime
        for key in ("StandardOutPath", "StandardErrorPath")
        if (raw := plist.get(key)) and (path := Path(str(raw))).exists()
    ]
    return max(stamps) if stamps else None


def _schedule(state: State) -> None:
    """Do the launchd jobs fire when the plists say they do?

    They did not, and nothing in this program could have told anyone. On 2026-08-11 all
    four calendar jobs were firing seven and a half hours early — the morning brief was
    written at 17:30 and the day planner ran at 17:17, planning a day that was over,
    which is why it kept reporting a fully booked day with a hundred items overflowing.
    The plists said 06:00 and 05:45 and `launchctl print` agreed with them, because the
    hour is not the thing that was wrong: launchd fixes a calendar job's fire times when
    it loads the job, and this owner had loaded them in one timezone and carried the
    machine to another five and a half hours away. Nothing re-evaluates on its own.

    Which is exactly the shape this module exists for. The schedule was a claim nobody
    could check: the file said one time, the job did another, and both looked right when
    read alone. So the claim here is not what the plist says — it is the distance between
    what it says and when the job was last seen to run. The remedy is `backglass schedule
    install`, which unloads and reloads each job and so re-fixes every fire time to the
    zone the machine is in now.
    """
    import plistlib
    from datetime import datetime

    if not LAUNCH_AGENTS_DIR.exists():
        state.add(
            "schedule", "jobs",
            Claim(None, str(LAUNCH_AGENTS_DIR), "no LaunchAgents dir"),
        )
        return
    paths = sorted(LAUNCH_AGENTS_DIR.glob(f"{JOB_PREFIX}*.plist"))
    if not paths:
        state.add(
            "schedule", "jobs",
            Claim([], f"{JOB_PREFIX}*.plist in {LAUNCH_AGENTS_DIR}",
                  "none installed — run `backglass schedule install`"),
        )
        return

    jobs: dict[str, str] = {}
    drifting: list[str] = []
    for path in paths:
        try:
            plist = plistlib.loads(path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - an unreadable plist is reportable news
            jobs[path.stem] = f"unreadable ({exc})"
            continue
        fired = _last_fire(plist)
        seen = datetime.fromtimestamp(fired).strftime("%H:%M") if fired else "never seen"
        calendar = plist.get("StartCalendarInterval")
        if isinstance(calendar, dict):
            hour, minute = int(calendar.get("Hour", 0)), int(calendar.get("Minute", 0))
            jobs[path.stem] = f"{hour:02d}:{minute:02d} daily, last ran {seen}"
            if fired is None:
                continue
            when = datetime.fromtimestamp(fired)
            # Circular: 23:50 against a 00:05 job is fifteen minutes apart, not
            # twenty-three hours, and a job that drifts across midnight is the case
            # this exists to catch rather than the one it should miss.
            apart = abs((when.hour * 60 + when.minute) - (hour * 60 + minute))
            if min(apart, 24 * 60 - apart) > SCHEDULE_DRIFT_TOLERANCE_MINUTES:
                drifting.append(f"{path.stem} fires {hour:02d}:{minute:02d}, last ran {seen}")
        elif (interval := plist.get("StartInterval")) is not None:
            # Interval jobs cannot drift: they count seconds, and seconds are the same
            # in every timezone. That is why the sync was the one job that stayed right.
            jobs[path.stem] = f"every {int(interval)}s, last ran {seen}"
        else:
            jobs[path.stem] = f"at load, last ran {seen}"

    state.add(
        "schedule", "jobs",
        Claim(jobs, f"{JOB_PREFIX}*.plist, and the mtime of each job's log"),
    )
    state.add(
        "schedule", "drifting",
        Claim(drifting,
              f"last fire more than {SCHEDULE_DRIFT_TOLERANCE_MINUTES}m from the "
              f"scheduled time — reinstall with `backglass schedule install`"),
    )
    state.add("schedule", "timezone", Claim(_timezone(), "readlink /etc/localtime"))


def _timezone() -> str | None:
    """The zone launchd will fix fire times to the next time a job is loaded."""
    try:
        return Path("/etc/localtime").resolve().as_posix().split("zoneinfo/", 1)[-1]
    except Exception:  # noqa: BLE001 - a missing link is unknown, not a crash
        return None


def collect(conn: sqlite3.Connection, settings: Settings) -> State:
    """Everything, read fresh. Each probe is independent: one failing must not blank
    the rest, because a partial truth that says which part is missing beats a total
    silence that says nothing."""
    state = State()
    probes: list[tuple[str, Any]] = [
        ("code", lambda: _code(state)),
        ("deployed", lambda: _deployed(state)),
        ("schema", lambda: _schema(conn, state)),
        ("prompts", lambda: _prompts(conn, state)),
        ("ledger", lambda: _ledger(conn, settings, state)),
        ("pipeline", lambda: _pipeline(conn, state)),
        ("knowledge_base", lambda: _knowledge_base(conn, settings, state)),
        ("open_questions", lambda: _open_questions(conn, state)),
        ("retrieval", lambda: _retrieval(conn, settings, state)),
        ("schedule", lambda: _schedule(state)),
    ]
    for name, probe in probes:
        try:
            probe()
        except Exception as exc:  # noqa: BLE001 - a probe's failure is reportable news
            # Named, because "a lambda raised" tells a reader nothing about which
            # section of the answer they are missing.
            state.add("errors", name, Claim(None, "probe raised",
                                            f"{type(exc).__name__}: {exc}"))
    return state


def as_json(state: State) -> str:
    return json.dumps(state.as_dict(), indent=2, sort_keys=True, default=str)
