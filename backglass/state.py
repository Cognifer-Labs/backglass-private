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

#: Written into the bundle by `desktop/build-sidecar.sh`, because PyInstaller compiles
#: the modules into an archive and leaves nothing on disk to hash. Without it the code —
#: the largest frozen surface there is — was the one this file never compared.
PYTHON_MANIFEST = "backglass-python.sha256"


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
    code_stale, code_note = _stale_python(INSTALLED_APP)
    state.add(
        "deployed", "matches_source",
        Claim(not stale and not missing and not code_stale and code_note is None,
              f"sha256 of {len(surfaces)} frozen surfaces and the Python manifest "
              "vs the checkout",
              "; ".join(f"{p} not found in the bundle" for p in missing) or code_note),
    )
    state.add("deployed", "stale_surfaces", Claim(stale, "sha256 mismatch vs the checkout"))
    state.add(
        "deployed", "stale_python",
        Claim(code_stale, f"sha256 vs {PYTHON_MANIFEST} recorded at build time", code_note),
    )


def _stale_python(app: Path) -> tuple[list[str], str | None]:
    """Which frozen Python modules differ from this checkout.

    The gap this closes: everything above hashes CSS, templates and scripts, and then
    `matches_source` reported True about an app running the previous week's planner —
    because PyInstaller compiles the modules into an archive, so there is nothing in the
    bundle to hash and nothing here ever looked. `state.py` already carries two comments
    about a list that reports a match on a surface it never examined; the surface it was
    itself missing was the code.

    A bundle cannot describe its own Python, so `build-sidecar.sh` records it at build
    time and this compares that record. An older app predating the manifest says so
    rather than passing — `unknown` over a confident answer assembled from a missing
    input is this module's whole contract.
    """
    manifest = app / "Contents/Resources/sidecar/backglass-server" / PYTHON_MANIFEST
    if not manifest.exists():
        return [], (
            f"{PYTHON_MANIFEST} is not in the bundle — it predates the manifest, so "
            "whether its code matches this checkout is unknown; rebuild to find out"
        )
    recorded: dict[str, str] = {}
    for line in manifest.read_text().splitlines():
        digest, _, relative = line.partition("  ")
        if digest and relative:
            recorded[relative.strip()] = digest.strip()
    if not recorded:
        return [], f"{PYTHON_MANIFEST} is empty"

    drifted = [
        relative for relative, digest in sorted(recorded.items())
        if _sha256(REPO_ROOT / relative) != digest
    ]
    # A file added to the checkout since the build is drift too: the app cannot be
    # running a module it was never given.
    here = {
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "backglass").rglob("*.py")
        if "__pycache__" not in path.parts
    }
    drifted.extend(sorted(here - set(recorded)))
    return drifted, None


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
            # Every stamp the file answers for: the current version plus its
            # `compatible:` list. A row stamped with a compatible old version is done
            # by that prompt's own declaration, so the citation check below must not
            # call it missing — @9 rows under a v10 file are the designed state, not
            # an edited-under-rows accident.
            stamps[path.stem] = ",".join(prompt_mod.load(path.stem).stamps)
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
    state.add("knowledge_base", "proposed",
              Claim(len(facts_mod.proposed(conn)),
                    "fact WHERE status = 'proposed' — extraction candidates waiting on"
                    " the owner, invisible to owner_context until accepted"))
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
    # Two causes, and they need different remedies, so the hint names both rather than
    # the one that happens to be commoner. A plist that was edited but never reloaded is
    # fixed by `schedule install`. A system whose calendar agent holds a stale timezone
    # is not: that agent reads the zone when it starts and never again, it is
    # SIP-protected against restarting, and a job loaded seconds ago inherits the stale
    # zone too — which is what happened here, and only a restart clears it.
    state.add(
        "schedule", "drifting",
        Claim(drifting,
              f"last fire more than {SCHEDULE_DRIFT_TOLERANCE_MINUTES}m from the "
              f"scheduled time — if every job is off by the same amount the machine's "
              f"timezone changed since it last booted and only a restart fixes it; "
              f"if one is off, `backglass schedule install` reloads it"),
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


# ── verdicts ──────────────────────────────────────────────────────────────
#
# `state` collected forty claims and judged none of them, so every reader — a person at
# a terminal, or an assistant told by CLAUDE.md to run this first — had to know which
# fields matter and what a bad value looks like. That knowledge lived nowhere. Six
# readings of this output in one session, each one hand-graded, is what a checker is for.
#
# Three boundaries keep it from becoming a different tool:
#
#   * **Additive.** The report is unchanged. Verdicts read the claims already collected;
#     nothing here re-derives a fact or adds a probe.
#   * **No live probes.** Everything above is an offline read — git, file hashes, db
#     counts, plists. A check that opened a socket or drove osascript would be `doctor`
#     wearing this file's name, and the three-tool line would blur: `status` is the human
#     glance, this is ground truth and the verdicts on it, `doctor` is the live probe.
#   * **Operational drift only.** A check earns its place by having a remedy someone can
#     run now. A standing design gap — the knowledge base's missing provenance, say — is
#     true, unfixable by a command, and would be red forever; a permanently red line is
#     one nobody reads, which is the failure this whole idea is trying to prevent.
#
# Deliberately NOT checked, each for a measured reason:
#
#   `schedule.drifting`   Non-empty every day on a machine that sleeps through 05:45 —
#                         that is the condition the catch-up net exists for, not a fault.
#                         The outcome is checked instead: does today have its plan and
#                         its brief, once their hour has passed.
#   `code.uncommitted`    Another session owns `tasks/lessons.md` on this checkout.
#   `commits_ahead`       Repo policy is never to push unprompted, so ahead is normal.
#   `retrieval.pending`   Transiently non-zero between syncs by design, and any threshold
#                         over it would be a number nobody chose.


@dataclass(frozen=True)
class Verdict:
    """One judgement, and the command that would clear it."""

    name: str
    ok: bool
    detail: str = ""
    remedy: str = ""
    #: A probe that could not run. Reported apart from a failure because they mean
    #: different things — but both exit non-zero, because this module's founding contract
    #: is that a confident answer assembled from a missing input is the thing to prevent.
    unknown: bool = False

    def line(self) -> str:
        mark = "[????]" if self.unknown else ("[ ok ]" if self.ok else "[FAIL]")
        text = f"{mark} {self.name}"
        if self.detail:
            text += f" — {self.detail}"
        if not self.ok and self.remedy:
            text += f"\n       fix: {self.remedy}"
        return text


def _value(state: State, section: str, name: str) -> Any:
    claim = state.sections.get(section, {}).get(name)
    return claim.value if claim else None


def verdicts(state: State, conn: sqlite3.Connection, settings: Settings) -> list[Verdict]:
    """Grade the collected claims. Pure — reads `state`, opens nothing new."""
    out: list[Verdict] = []

    # Any probe that raised took a whole section with it, so the fields it would have
    # graded are absent rather than green.
    for name, claim in state.sections.get("errors", {}).items():
        out.append(Verdict(f"{name} probe", ok=False, unknown=True,
                           detail=claim.unknown or "probe raised",
                           remedy="re-run; if it persists the section's reader is broken"))

    # An `unknown` anywhere is the founding case: the value is meaningless and must not
    # be read as a pass. Its own string is the remedy, which is why probes are required
    # to say why rather than merely that.
    for section, claims in state.sections.items():
        if section == "errors":
            continue
        for name, claim in claims.items():
            if claim.unknown:
                out.append(Verdict(f"{section}.{name}", ok=False, unknown=True,
                                   detail=claim.unknown))

    applied = _value(state, "schema", "applied")
    on_disk = _value(state, "schema", "on_disk")
    if isinstance(applied, int) and isinstance(on_disk, int):
        if applied < on_disk:
            out.append(Verdict("schema is current", ok=False,
                               detail=f"{on_disk - applied} migration(s) on disk not applied",
                               remedy="any `backglass` command applies them at startup"))
        elif applied > on_disk:
            # The 2026-08-13 crash, before it crashes: the db has run a migration this
            # checkout does not have, so whatever is reading it will refuse to start.
            out.append(Verdict("schema is current", ok=False,
                               detail=f"the database has {applied - on_disk} migration(s) "
                                      "this checkout does not, so migrate() will refuse",
                               remedy="check out the newer code, or restore the db backup"))
        else:
            out.append(Verdict("schema is current", ok=True, detail=f"{applied} applied"))

    if _value(state, "deployed", "app") is not None:
        stale = list(_value(state, "deployed", "stale_surfaces") or [])
        stale += list(_value(state, "deployed", "stale_python") or [])
        if stale:
            out.append(Verdict("installed app matches this checkout", ok=False,
                               detail=f"{len(stale)} stale: {', '.join(stale[:3])}"
                                      + ("…" if len(stale) > 3 else ""),
                               remedy="./desktop/build-sidecar.sh, then copy the bundle "
                                      "over /Applications/Backglass.app"))
        elif _value(state, "deployed", "matches_source") is True:
            out.append(Verdict("installed app matches this checkout", ok=True))

    # A plist on disk is a wish; only a loaded label is a schedule. This is the gap
    # that silently stopped the ledger for thirteen hours on 2026-08-18: the sync
    # plist existed, its log had an mtime, and launchd had never been asked.
    try:
        from backglass import schedule as schedule_mod

        unloaded = schedule_mod.unloaded_jobs()
    except Exception as exc:  # noqa: BLE001 — unknown, never a silent pass
        unloaded = None
        unloaded_why = f"{type(exc).__name__}: {exc}"
    else:
        unloaded_why = "launchctl could not be asked"
    if unloaded is None:
        out.append(Verdict("every scheduled job is loaded", ok=False, unknown=True,
                           detail=unloaded_why))
    else:
        out.append(Verdict(
            "every scheduled job is loaded",
            ok=not unloaded,
            detail=", ".join(unloaded) if unloaded else "",
            remedy="open the Backglass app (it re-loads them at startup), or"
                   " `backglass schedule install`",
        ))

    # A version the ledger has extracted with but that is no longer on disk: the prompt
    # was edited or renamed under rows that cite it, so nothing can reproduce them.
    on_disk_versions = {
        stamp
        for joined in (_value(state, "prompts", "on_disk") or {}).values()
        for stamp in str(joined).split(",")
    }
    ledger_versions = [
        v for v in (_value(state, "prompts", "versions_in_the_ledger") or [])
        if v and v != "manual"
    ]
    missing = sorted(v for v in ledger_versions if v not in on_disk_versions)
    out.append(Verdict(
        "every prompt the ledger cites is on disk",
        ok=not missing,
        detail=", ".join(missing) if missing else f"{len(ledger_versions)} in use",
        remedy="restore the prompt file, or re-extract the rows citing it",
    ))

    for field, label in (("untriaged", "triaged"), ("kept_not_extracted", "extracted")):
        count = _value(state, "ledger", field)
        if isinstance(count, int):
            out.append(Verdict(
                f"every kept item is {label}",
                ok=count == 0,
                detail=f"{count} waiting" if count else "nothing waiting",
                remedy="`backglass sync` — or the spend cap stopped the run short",
            ))

    last = _value(state, "pipeline", "last_run") or {}
    if last:
        out.append(Verdict(
            "last run completed without degrading",
            ok=not last.get("degraded"),
            detail=str(last.get("degrade_reason") or "") or f"run {last.get('id')}",
            remedy="raise MAX_SPEND_PER_RUN_USD, or let the cap reset",
        ))

    drift = _value(state, "knowledge_base", "config_drift") or []
    out.append(Verdict("config agrees with the knowledge base", ok=not drift,
                       detail="; ".join(str(d) for d in drift[:2]),
                       remedy="`backglass memory` — the drift line names the field"))

    out.extend(_morning_verdicts(conn, settings))
    return out


def _morning_verdicts(
    conn: sqlite3.Connection, settings: Settings
) -> list[Verdict]:
    """Did today's surfaces actually arrive?

    The outcome, deliberately, rather than the mechanism. `schedule.drifting` is
    non-empty every single day on a machine that sleeps through 05:45 — launchd defers a
    missed calendar interval to the next wake — and a check on it would be red forever
    while the product worked fine, because the catch-up net fills the hole on the first
    sync. What matters is not whether the job fired at 05:45 but whether the plan and the
    brief exist now that their hour has passed.
    """
    from backglass import catchup, heartbeat
    from backglass.plan import timezones

    out: list[Verdict] = []
    try:
        now = timezones.local_now(settings)
        today = now.date()
        beat = heartbeat.read(conn, settings, today, now)
    except Exception as exc:  # noqa: BLE001 — unknown, never a silent pass
        return [Verdict("today's plan and brief", ok=False, unknown=True,
                        detail=f"{type(exc).__name__}: {exc}")]

    if beat.plan_due:
        out.append(Verdict(
            "today has a plan",
            ok=not beat.plan_missing,
            detail="none for " + today.isoformat() if beat.plan_missing else "",
            remedy="`backglass plan` — or the next sync's catch-up will fill it",
        ))
    if catchup._owed(now, settings.brief_at) and timezones.is_working_day(settings, today):
        missing = catchup.brief_is_missing(conn, today)
        out.append(Verdict(
            "today has a brief",
            ok=not missing,
            detail="none for " + today.isoformat() if missing else "",
            remedy="`backglass brief` — or the next sync's catch-up will fill it",
        ))
    return out


def as_json(state: State, checks: list[Verdict] | None = None) -> str:
    payload = state.as_dict()
    if checks is not None:
        payload["verdicts"] = [
            {"name": v.name, "ok": v.ok, "unknown": v.unknown,
             "detail": v.detail, "remedy": v.remedy}
            for v in checks
        ]
    return json.dumps(payload, indent=2, sort_keys=True, default=str)
