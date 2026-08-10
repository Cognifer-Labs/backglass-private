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
    return (*FROZEN_STYLESHEETS, *templates)


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
