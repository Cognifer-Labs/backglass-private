"""The command line. docs/10 §CLI.

The whole system is operated from here and the surface is deliberately small. `--dry-run`
on sync is a hard requirement rather than a convenience: it prints the diff and writes
nothing, and it is what makes every phase after this one debuggable.
"""

from __future__ import annotations

import ipaddress
import json
import sqlite3
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, Any

import typer

from backglass import loop
from backglass.brief import model
from backglass.config import Settings, get_settings
from backglass.connectors import credentials
from backglass.connectors.base import Connector
from backglass.connectors.boundary import Boundary, purge
from backglass.connectors.gmail import SCOPES, GmailConnector
from backglass.db import connect, migrate, now_iso, query
from backglass.extract import client as model_client
from backglass.extract import prompts
from backglass.goals import activities as activities_mod
from backglass.ledger import USER_ID
from backglass.sync import EXTRACT_PROMPT, SyncLocked, record_run, sync

app = typer.Typer(
    add_completion=False,
    help="Backglass — a commitment ledger with documents as evidence.",
    no_args_is_help=True,
)


def _open(settings: Settings) -> sqlite3.Connection:
    return connect(settings.db_path)


def _build_model_client(settings: Settings, conn: Any | None = None) -> Any:
    """`model_client.build()`, but a missing key/CLI degrades (Rule 5) instead of an
    uncaught `ModelError` producing a full stack trace for a fresh, unconfigured clone.

    `conn` is optional and only buys the measured routing order: with one, the provider's
    fallback array leads with whatever has recently been answering. Without one the body
    is the configured order, which is what it always was — a caller that has no database
    open must not be made to open one to send a prompt.
    """
    routes = None
    if conn is not None:
        # Rule 5 in its own right. Routing is an optimisation over telemetry, and a
        # pipeline that will not run because it could not read its own call history
        # would be the reporting layer becoming load-bearing.
        try:
            from backglass import modelhealth

            routes = modelhealth.routes(conn, settings)
        except Exception:  # noqa: BLE001
            routes = None
    try:
        return model_client.build(settings, routes=routes)
    except model_client.ModelError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def init() -> None:
    """Create the database and run migrations. Safe to run repeatedly."""
    settings = get_settings()
    conn = _open(settings)
    applied = migrate(conn)
    if applied:
        typer.echo(f"applied migration(s): {', '.join(f'{v:04d}' for v in applied)}")
    else:
        typer.echo("schema already up to date")
    typer.echo(f"database: {settings.db_path}")


imessage_app = typer.Typer(help="The local Messages store.")
app.add_typer(imessage_app, name="imessage")


@imessage_app.command("prune")
def imessage_prune(
    apply_it: Annotated[bool, typer.Option("--apply", help="Actually delete")] = False,
) -> None:
    """Remove stored messages the current IMESSAGE_CHATS and window no longer admit.

    Raw items are kept forever (docs/03) with one sanctioned exception: content the owner
    has decided this system may not hold (docs/08). Narrowing an allowlist is that same
    decision, so it goes through the same delete gate — otherwise the setting only ever
    applies to messages not yet read, and everything captured under a looser rule stays.

    Dry run unless `--apply`.
    """
    from contextlib import closing

    from backglass.connectors.allowlist import Allowlist
    from backglass.connectors.imessage import prune

    settings = get_settings()
    with closing(connect(settings.db_path)) as conn:
        report = prune(
            conn,
            Allowlist(settings.imessage_chats),
            lookback_days=settings.imessage_lookback_days,
            dry_run=not apply_it,
        )
    verb = "removed" if apply_it else "would remove"
    typer.echo(f"scanned {report.scanned}, {verb} {report.removed}, kept {report.kept}")
    for chat, n in sorted(report.by_chat.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {n:>6}  {chat}")
    if report.removed and not apply_it:
        typer.echo("\nnothing was deleted — re-run with --apply")


@imessage_app.command("chats")
def imessage_chats(
    days: Annotated[int, typer.Option("--days", help="Window to summarise")] = 90,
) -> None:
    """List conversations in the store, busiest first, for IMESSAGE_CHATS.

    The allowlist is spelled in display names and handles, and neither is something
    anyone recalls exactly — a group is "Pih ball" or "pih Ball" depending on who named
    it, and a one-to-one is a raw phone number. Guessing is how an allowlist ends up
    matching nothing at all, so the names are read off the store. Nothing is ingested by
    this command; it counts, records the conversations as awaiting a decision, and prints.

    The scan is the connector's own `discover()` rather than a second copy of the query,
    so what this prints and what the /chats page offers cannot disagree about which
    conversations exist or what they are called.
    """
    import sqlite3 as _sqlite

    from backglass import chats as chats_mod
    from backglass.connectors.allowlist import normalise
    from backglass.connectors.imessage import IMessageConnector

    settings = get_settings()
    if not settings.imessage_db_path:
        typer.secho("IMESSAGE_DB_PATH is not set — run `backglass setup`", fg=typer.colors.RED)
        raise typer.Exit(1)

    connector = IMessageConnector(
        db_path=settings.imessage_db_path,
        boundary=Boundary.from_settings(settings),
        lookback_days=days,
    )
    try:
        seen = connector.discover()
    except _sqlite.Error as exc:
        typer.secho(f"cannot read the Messages store: {exc}", fg=typer.colors.RED)
        typer.echo("  grant Full Disk Access to this terminal and to the uv binary")
        raise typer.Exit(1) from exc

    conn = _open(settings)
    migrate(conn)
    chats_mod.seed_from_env(conn, "imessage", settings.imessage_chats)
    chats_mod.record(conn, "imessage", list(seen.values()), cumulative=False)
    # The stored rows rather than the raw sightings, because those carry the address
    # book's answer to "who is +14802411748" — the thing this list exists to tell you.
    stored = {c.key: c for c in chats_mod.listing(conn, "imessage")}

    typer.echo(f"Conversations in the last {days} days — decide at /chats:\n")
    for sighting in sorted(seen.values(), key=lambda s: -s.messages):
        row = stored.get(normalise(sighting.key))
        decision = (row.decision if row else None) or ""
        mark = {"monitor": "on ", "ignore": "off"}.get(decision, "   ")
        kind = "group" if sighting.kind == "group" else "1:1  "
        label = row.label if row else sighting.display_name
        handle = f"  ({row.identifier})" if row and row.identifier else ""
        typer.echo(f"  {mark} {kind}  {sighting.messages:>6}  {label}{handle}")
    undecided = sum(
        1 for s in seen if (stored.get(normalise(s)) is None or stored[normalise(s)].undecided)
    )
    if undecided:
        typer.echo(
            f"\n{undecided} conversation(s) awaiting a decision. "
            "Choose at /chats — none is read until it is chosen."
        )


instagram_app = typer.Typer(help="The experimental live Instagram lane.")
app.add_typer(instagram_app, name="instagram")


@instagram_app.command("login")
def instagram_login(
    username: Annotated[str, typer.Option("--username", help="Your Instagram handle")] = "",
    session_file: Annotated[
        Path, typer.Option("--session-file", help="Where to store the session")
    ] = Path("data/instagram-session.json"),
    env_path: Annotated[
        Path, typer.Option("--env-path", help="Env file to write")
    ] = Path(".env"),
) -> None:
    """Log in once and keep the session, so no run ever logs in again.

    This is the whole safety story for the live lane, such as it is. Instagram's private
    API tolerates a client that reuses a session and reacts badly to one that
    re-authenticates; `docs/07` calls never re-logging-in the single biggest thing under
    the owner's control. So the password is asked for exactly once, here, interactively —
    it is never written to .env, never passed as an argument where a shell history would
    keep it, and never held after the session is saved.

    The lane still drives an interface Meta does not publish and its terms do not permit.
    That trade is the owner's to make; this command only makes the safer half of it easy.
    """
    from backglass import envfile

    try:
        from instagrapi import Client
    except ImportError as exc:
        typer.secho("instagrapi is not installed.", fg=typer.colors.RED)
        typer.echo("  uv sync --extra instagram")
        raise typer.Exit(1) from exc

    settings = get_settings()
    username = username or settings.instagram_username or typer.prompt("Instagram username")
    password = typer.prompt("Instagram password", hide_input=True)

    client = Client()
    # instagrapi asks for the 2FA code through this callback rather than by raising, so
    # the prompt has to be handed in up front or a challenged login dies mid-flow.
    client.challenge_code_handler = lambda _u, _c: typer.prompt("Verification code")
    client.change_password_handler = lambda _u: typer.prompt(
        "New password", hide_input=True
    )
    try:
        client.login(username, password)
    except Exception as exc:  # noqa: BLE001 — every failure here is a product state
        typer.secho(f"login failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    finally:
        del password

    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.touch(exist_ok=True)
    envfile.restrict(session_file)  # owner-only BEFORE the session lands in it
    client.dump_settings(session_file)

    changed = envfile.set_keys(
        env_path,
        {"INSTAGRAM_USERNAME": username, "INSTAGRAM_SESSION_FILE": str(session_file)},
    )
    typer.secho(
        f"session saved to {session_file} ({len(changed)} env change(s))",
        fg=typer.colors.GREEN,
    )
    typer.echo("next: uv run backglass instagram chats   # to pick which threads to read")


@instagram_app.command("chats")
def instagram_chats(
    limit: Annotated[int, typer.Option("--limit", help="How many threads to list")] = 30,
) -> None:
    """List thread titles — the same names a sync discovers onto the /chats page.

    The live lane reads an allowlist, never an inbox — `docs/07`: "a personal tool reads
    the handful of threads the owner names." Guessing those names from memory is how an
    allowlist ends up silently matching nothing, so they are read off the account itself.
    Nothing is stored by this command; it prints and exits.
    """
    try:
        from instagrapi import Client
    except ImportError as exc:
        typer.secho("instagrapi is not installed.", fg=typer.colors.RED)
        typer.echo("  uv sync --extra instagram")
        raise typer.Exit(1) from exc

    settings = get_settings()
    if not settings.instagram_session_file or not settings.instagram_session_file.exists():
        typer.secho(
            "no session file — run `backglass instagram login` first", fg=typer.colors.RED
        )
        raise typer.Exit(1)

    client = Client()
    client.load_settings(settings.instagram_session_file)
    try:
        threads = client.direct_threads(amount=limit)
    except Exception as exc:  # noqa: BLE001 — rule 5: degrade, never blow up
        typer.secho(f"could not list threads: {exc}", fg=typer.colors.RED)
        typer.echo("  a session can be invalidated by Instagram; re-run `instagram login`")
        raise typer.Exit(1) from exc

    typer.echo(
        "Choose the ones worth reading on the /chats page — a sync discovers these "
        "same threads and lists them there for a Monitor/Ignore decision:\n"
    )
    for thread in threads:
        title = getattr(thread, "thread_title", "") or ", ".join(
            getattr(user, "username", "?") for user in getattr(thread, "users", [])
        )
        people = len(getattr(thread, "users", []))
        typer.echo(f"  {title}    ({people} participant{'' if people == 1 else 's'})")


@app.command(name="google-client")
def google_client(
    path: Annotated[Path, typer.Argument(help="The client_secret_*.json Google gave you")],
    env_path: Annotated[
        Path, typer.Option("--env-path", help="Env file to write")
    ] = Path(".env"),
) -> None:
    """Load a downloaded Google OAuth client into .env, refusing the wrong kind.

    Google's Credentials page will happily hand you a *web* client, and this machine
    already had one sitting in ~/Downloads from an unrelated project. A web client has no
    localhost redirect, so `backglass auth` fails deep inside the consent flow with an
    error about redirect URIs that says nothing about the actual mistake. The type is
    checked here, where the fix ("create a Desktop app client") can be stated plainly.

    The secret is read from the file and written straight to .env — it is never printed,
    and the file it came from is left alone for you to delete.
    """
    import json as _json

    from backglass import envfile

    try:
        payload = _json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        typer.secho(f"cannot read {path}: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    kind = next(iter(payload), "")
    if kind != "installed":
        typer.secho(
            f"{path.name} is a '{kind or 'unknown'}' OAuth client, not a Desktop app one.",
            fg=typer.colors.RED,
        )
        typer.echo(
            "  backglass runs the consent flow on a short-lived local redirect, which\n"
            "  only a Desktop app client allows. In the Google Cloud console:\n"
            "    APIs & Services -> Credentials -> Create credentials -> OAuth client ID\n"
            "    Application type: Desktop app\n"
            "  then download that JSON and run this again."
        )
        raise typer.Exit(1)

    client = payload[kind]
    client_id, secret = client.get("client_id", ""), client.get("client_secret", "")
    if not client_id or not secret:
        typer.secho(f"{path.name} has no client_id/client_secret", fg=typer.colors.RED)
        raise typer.Exit(1)

    changed = envfile.set_keys(
        env_path, {"GOOGLE_CLIENT_ID": client_id, "GOOGLE_CLIENT_SECRET": secret}
    )
    typer.secho(
        f"wrote {len(changed) or 'no'} change(s) to {env_path}"
        f" (project {client.get('project_id', '?')})",
        fg=typer.colors.GREEN,
    )
    typer.echo("next: uv run backglass auth <label> --source gmail")


@app.command()
def auth(
    account: Annotated[str, typer.Argument(help="Label, e.g. 'personal'")],
    source: Annotated[str, typer.Option("--source", help="gmail | calendar | drive")] = "gmail",
) -> None:
    """Run the Google OAuth consent flow for one account and store the tokens.

    Read-only scopes, and nothing else. docs/08: this system never sends or modifies, and
    docs/07 §Calendar is explicit that the planner "proposes; it never writes".
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    settings = get_settings()
    if not settings.google_client_id or not settings.google_client_secret:
        typer.echo("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env", err=True)
        raise typer.Exit(2)

    from backglass.connectors.calendar import SCOPES as CAL_SCOPES
    from backglass.connectors.drive import SCOPES as DRIVE_SCOPES

    scopes = {"gmail": SCOPES, "calendar": CAL_SCOPES, "drive": DRIVE_SCOPES}.get(source)
    if scopes is None:
        typer.echo(f"unknown source {source!r}; expected gmail, calendar or drive", err=True)
        raise typer.Exit(2)

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=scopes,
    )
    creds = flow.run_local_server(port=0)
    conn = _open(settings)
    credentials.save_tokens(
        conn,
        f"{source}:{account}",
        access_token=creds.token,
        refresh_token=creds.refresh_token,
        expires_at=creds.expiry.isoformat() if creds.expiry else None,
        scopes=" ".join(scopes),
    )
    typer.echo(f"stored credentials for {source}:{account}")


@app.command("sync")
def sync_command(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the diff, write nothing")
    ] = False,
) -> None:
    """Ingest, triage, extract."""
    settings = get_settings()
    conn = _open(settings)
    started_at = now_iso()
    try:
        migrate(conn)
    except Exception as exc:
        # rule 5, one layer earlier than sync() itself can reach: a schema mismatch
        # (found 2026-09-08 — a working-tree migration rename collision) throws here,
        # before `sync()` ever runs, so nothing would otherwise land in `run` at all.
        # 196 crashes across roughly three hours left zero trace, and `state`'s own
        # "last run completed without degrading" check kept reading the last *successful*
        # row from before the outage as current, because there was nothing newer to read.
        # A degraded row here is what that check needs to catch the very next cycle
        # instead of staying blind until someone reads `sync.err` by hand. Re-raised
        # after recording, so the traceback and the non-zero exit are unchanged.
        #
        # Best-effort, same as `_record_repair_errors` below: a *first-ever* migration
        # failing before the `run` table exists yet would make this INSERT itself throw,
        # and a failure to record a failure must never replace the original traceback
        # with a more confusing one about a missing table.
        try:
            record_run(
                conn,
                started_at=started_at,
                degraded=True,
                degrade_reason=f"migrate() failed: {type(exc).__name__}: {exc}",
            )
            conn.commit()
        except Exception:  # noqa: BLE001 — rule 5, and the real error raises right below
            pass
        raise
    connectors = _all_connectors(conn, settings)
    if not connectors:
        typer.echo("no sources configured; run `backglass auth <label>` first", err=True)
        raise typer.Exit(2)

    try:
        report = sync(
            conn,
            settings,
            connectors,
            _build_model_client(settings, conn),
            dry_run=dry_run,
            contacts_source=_contacts_source(conn, settings),
        )
    except SyncLocked as locked:
        # Expected under launchd: a 30-minute timer will sometimes fire mid-backfill.
        # The other run is doing the work, so this is a clean skip, not a failure.
        typer.echo(f"{locked}; skipped")
        raise typer.Exit(0) from None
    _print_report(report, dry_run=dry_run)
    if not dry_run:
        # The loop under the pipeline: the morning surfaces caught up, a drifted plan
        # refreshed, the record's own contradictions disposed of, the detectors asked,
        # and the day's notifications delivered.
        #
        # The list, the order, the lock and rule 5 all live in `backglass/loop.py` now.
        # They were a hundred lines of `try/except: pass` here, which is why the app-open
        # trigger ran one of these five passes and this ran all of them with nothing
        # anywhere to compare the two against.
        outcomes = loop.run(conn, settings, run_id=report.run_id)
        for outcome in outcomes:
            for line in outcome.lines:
                typer.echo(line)
            if outcome.error and outcome.status == loop.FAILED:
                # Rule 5 in full: log, surface, continue, exit non-zero. The old shape
                # implemented "continue" and nothing else, so a detector throwing for a
                # week read exactly like a detector with nothing to find.
                typer.echo(f"loop pass '{outcome.name}' failed: {outcome.error}", err=True)
        if any(o.status == loop.FAILED for o in outcomes) and not report.exit_code:
            raise typer.Exit(1)
    raise typer.Exit(report.exit_code)


@app.command("loop")
def loop_command(
    only: Annotated[
        list[str] | None,
        typer.Option("--only", help="Run just these passes; repeatable"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print where each pass stands, run nothing"),
    ] = False,
) -> None:
    """Run the passes that follow a sync — catchup, replan, logic, questions, notify.

    One-shot, always. `backglass sync` already runs these every thirty minutes, so this
    exists for the two cases it does not cover: reading where the loop stands, and
    nudging it after changing something the passes read. The point of the rest of goal 3
    is that the owner never needs to type it.

    **Not a scheduler.** launchd owns cadence (CLAUDE.md's decisions table); this runs
    what is owed now, once, and exits. It does not sleep, and it must not learn to.
    """
    settings = get_settings()
    conn = _open(settings)
    migrate(conn)

    try:
        passes = loop.by_name(only) if only else None
    except KeyError as exc:
        typer.echo(str(exc).strip("'"), err=True)
        raise typer.Exit(2) from None

    if dry_run:
        # Deliberately not "run everything and roll back". These passes write through
        # planner, notify and the detectors, each with its own idempotency rule; a
        # rehearsal that actually called them would deliver notifications and supersede
        # plans, which is not what "dry run" means to anyone reading it. What the owner
        # wants from this flag is where the loop stands, and that is a read.
        for entry, health in zip(loop.PASSES, loop.health(conn), strict=True):
            if passes is not None and entry not in passes:
                continue
            mark = "never run" if health.last_ok is None else f"last ok {health.last_ok}"
            typer.echo(f"{entry.name:<10} {entry.trigger:<7} {mark}")
            if health.failing:
                typer.echo(
                    f"           failing ×{health.consecutive_failures}: {health.last_error}"
                )
        raise typer.Exit(0)

    outcomes = loop.run(conn, settings, passes=passes)
    for outcome in outcomes:
        for line in outcome.lines:
            typer.echo(line)
        if outcome.status == loop.FAILED:
            typer.echo(f"loop pass '{outcome.name}' failed: {outcome.error}", err=True)
        elif outcome.status == loop.SKIPPED:
            # Said out loud here, unlike in `sync`, because someone typed this and is
            # waiting on it: silence after an explicit command reads as "it ran and found
            # nothing", which is the opposite of what happened.
            typer.echo(f"skipped: {outcome.error}")
    if all(o.quiet for o in outcomes):
        typer.echo("nothing owed")
    raise typer.Exit(1 if any(o.status == loop.FAILED for o in outcomes) else 0)


@app.command("contacts")
def contacts_command(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print what would link, write nothing")
    ] = False,
) -> None:
    """Name the numbers: fold Contacts.app into the people the ledger already knows."""
    from backglass import contacts as contacts_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    source = _contacts_source(conn, settings)
    if source is None:
        # Two ways to be off, and they need different instructions — a pause the owner
        # chose must not be reported as missing configuration they then go and re-add.
        from backglass.connectors.contacts import SOURCE as CONTACTS_SOURCE

        if CONTACTS_SOURCE in credentials.disabled_sources(conn):
            typer.secho(
                f"{CONTACTS_SOURCE} is paused — `backglass sources enable "
                f"{CONTACTS_SOURCE}` to read the address book again",
                fg=typer.colors.YELLOW,
            )
        else:
            typer.secho("APPLE_CONTACTS is not set — add it to .env", fg=typer.colors.RED)
        raise typer.Exit(2)
    health = source.health()
    if not health.ok:
        typer.secho(health.detail or "contacts unavailable", fg=typer.colors.RED)
        raise typer.Exit(1)

    report = contacts_mod.import_contacts(conn, source.read(), dry_run=dry_run)
    conn.commit()
    typer.echo(
        f"read {report.read} contacts — {report.entities_created} new people, "
        f"{report.entities_updated} updated, {report.aliases_added} identifiers linked"
    )
    if report.excluded:
        typer.echo(f"  boundary excluded {report.excluded}")
    for name in report.ambiguous:
        # Named rather than resolved: two cards claiming one number is a question for
        # the owner, and a wrong name is worse than a bare number.
        typer.echo(f"  ambiguous, left unlinked: {name}")

    from backglass import chats as chats_mod

    chats = chats_mod.listing(conn)
    resolved = contacts_mod.resolve(conn, [chat.key for chat in chats])
    named = sum(1 for r in resolved.values() if r.name)
    typer.echo(
        f"conversations: {len(chats)} known, {named} now named, "
        f"{len(resolved) - named} still a bare identifier"
    )


@app.command()
def commitments(
    review: Annotated[bool, typer.Option("--review", help="Only the review queue")] = False,
) -> None:
    """The Phase 1 acceptance query: open commitments, each with its source."""
    settings = get_settings()
    conn = _open(settings)
    rows = list(
        conn.execute(
            query("open_commitments"),
            {"user_id": USER_ID, "confidence_threshold": settings.confidence_threshold},
        )
    )
    if review:
        rows = [row for row in rows if row["needs_review"]]
    if not rows:
        typer.echo("no open commitments")
        return
    for row in rows:
        arrow = "→" if row["direction"] == "i_owe" else "←"
        due = row["due_at"] or "no date"
        flag = "  [review]" if row["needs_review"] else ""
        typer.echo(f"{arrow} {row['what']}  ({row['counterparty'] or 'unknown'})")
        typer.echo(
            f"    due {due} · confidence {row['confidence']:.2f}{flag}\n"
            f"    source: {row['source']} {row['source_external_id']} "
            f"({row['source_occurred_at']}) — {row['source_title'] or 'no subject'}"
        )


@app.command()
def errors(
    runs: Annotated[
        int, typer.Option("--runs", help="How many recent sync runs to read")
    ] = 0,
) -> None:
    """Everything the pipeline logged as gone wrong across the recent sync runs.

    `status` shows the last run's errors and only those; the next sync, thirty minutes
    later, replaces them. This reads the same window the Sources panel does, grouped the
    same way, so the panel's "N other errors" has somewhere to point. It is the full
    list, uncapped — the panel is a glance surface and has to stop somewhere, this does
    not.
    """
    from backglass.web.panels import ERROR_WINDOW_RUNS, error_line

    settings = get_settings()
    conn = _open(settings)
    rows = list(
        conn.execute(
            query("recent_run_errors"),
            {"user_id": USER_ID, "runs": runs or ERROR_WINDOW_RUNS},
        )
    )
    if not rows:
        window = min(runs or ERROR_WINDOW_RUNS, _sync_run_count(conn))
        typer.echo(f"no errors in the last {window} sync run(s)")
        return
    typer.echo(f"errors in the last {rows[0]['window_runs']} sync run(s)")
    for row in rows:
        typer.echo(f"  {error_line(row)}")


def _sync_run_count(conn: sqlite3.Connection) -> int:
    """How many sync runs exist at all, so the no-errors line can be as honest about its
    window as the grouped rows are about theirs."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM run WHERE user_id = ? AND kind = 'sync'", (USER_ID,)
    ).fetchone()
    return int(row["n"]) if row else 0


@app.command()
def status() -> None:
    """Connector health, triage kill rate, spend, last run. The Sources panel, in text."""
    settings = get_settings()
    conn = _open(settings)

    typer.echo("sources")
    rows = list(conn.execute("SELECT source, status, last_error, updated_at FROM credential"))
    if not rows:
        typer.echo("  none configured")
    for row in rows:
        mark = "ok " if row["status"] == "ok" else "FAIL"
        typer.echo(f"  [{mark}] {row['source']}  updated {row['updated_at']}")
        if row["last_error"]:
            typer.echo(f"         {row['last_error']}")

    # Sources with evidence but no credential row — quick-adds, imports, a connector
    # whose credential was removed. They cite into the brief like anything else and no
    # sync will ever refresh them, so `status` names them instead of reading "none".
    for row in conn.execute(query("unmanaged_sources"), {"user_id": USER_ID}):
        typer.echo(
            f"  [ -- ] {row['source']}  {row['item_count']} item(s), no connector — "
            f"last {row['last_item_at']}"
        )

    kill = conn.execute(query("triage_kill_rate"), {"user_id": USER_ID}).fetchone()
    if kill and kill["total"]:
        rate = float(kill["kill_rate"] or 0.0)
        warn = "  ← below 85%, rules have drifted" if rate < 0.85 else ""
        typer.echo(f"\ntriage  {kill['dropped']}/{kill['total']} dropped ({rate:.0%}){warn}")

    last = conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        # `status` is the one reader that deliberately does not migrate: a diagnostic must
        # not mutate the store it is diagnosing, which is exactly what you want of the
        # command you reach for when something is wrong. So it cannot assume a column
        # added by 0016 exists — a restored pre-0016 backup whose last run was degraded
        # would otherwise die of KeyError in the one command asked to explain it.
        # dict() rather than `in last.keys()`: SIM118 would rewrite that to `in last`,
        # and on a sqlite3.Row `in` tests VALUES, not keys — it is False for a column
        # that exists and True for the string 'spend_cap' itself.
        reason = dict(last).get("degrade_reason") or "spend_cap"
        typer.echo(
            f"\nlast run  {last['started_at']}  fetched {last['items_fetched']}, "
            f"extracted {last['items_extracted']}, writes {last['writes']}, "
            f"spend {last['spend_cents']}c"
            + (f"  DEGRADED ({reason})" if last["degraded"] else "")
        )
        if last["errors_json"]:
            for error in json.loads(str(last["errors_json"])):
                typer.echo(f"    {error}")


@app.command()
def extract(
    reprocess: Annotated[
        bool, typer.Option("--reprocess", help="Clear stamps and re-extract")
    ] = False,
    version: Annotated[
        str | None, typer.Option("--version", help="Only items at this stamp")
    ] = None,
) -> None:
    """Re-run extraction against a new prompt version. Nothing is re-fetched.

    docs/02 §Immutable source items: "Bumping the version and re-running produces a diff
    you can inspect before accepting."
    """
    settings = get_settings()
    conn = _open(settings)
    if reprocess:
        if version:
            cursor = conn.execute(
                "UPDATE source_item SET extraction_version = NULL "
                "WHERE user_id = ? AND extraction_version = ?",
                (USER_ID, version),
            )
        else:
            cursor = conn.execute(
                "UPDATE source_item SET extraction_version = NULL "
                "WHERE user_id = ? AND triage_verdict = 'keep'",
                (USER_ID,),
            )
        typer.echo(f"cleared {cursor.rowcount} extraction stamp(s)")

    current = prompts.load(EXTRACT_PROMPT).stamp
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM source_item WHERE user_id = ? AND triage_verdict = 'keep' "
        "AND (extraction_version IS NULL OR extraction_version != ?)",
        (USER_ID, current),
    ).fetchone()
    typer.echo(f"{pending['n']} item(s) pending at {current}; run `backglass sync` to extract")


@app.command("state")
def state_command(
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable, every claim with its derivation")
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", help="The verdicts alone, without the full report")
    ] = False,
) -> None:
    """Ground truth about this installation, with how each claim was derived.

    `status` answers "is it healthy" for a person. This answers "what IS it" for
    anything that has to check rather than trust — including a future session of an
    assistant, which is what it was written for. Every field names the query, file or
    command behind it, so a reader can re-derive instead of believing; a probe that
    cannot run says `unknown` and why, because a confident answer assembled from a
    missing input is the failure this exists to prevent.

    It also grades what it collected, and **exits non-zero when something needs doing**.
    Reporting forty fields and judging none of them left every reader to know which ones
    matter and what a bad value looks like, and that knowledge was written down nowhere.
    The verdicts read the claims above and add no probe of their own: `status` stays the
    human glance, this is ground truth plus the judgement on it, and `doctor` remains the
    only one that touches a live surface.

    An `unknown` exits non-zero too. The value is meaningless, and treating "I could not
    tell" as a pass is precisely the failure this module was written to prevent.

    `--quiet` prints the verdicts alone, for a pre-flight that only cares whether
    anything is wrong.

    Nothing here is cached. Every field is read at call time.
    """
    from backglass import state as state_mod

    settings = get_settings()
    # Deliberately NOT `migrate(conn)`, unlike every other command that opens the ledger.
    #
    # Two reasons, and the second is why the first was not noticed for so long. This is
    # the command CLAUDE.md tells a reader to run *before trusting anything about the
    # installation*, and a report that mutates what it is reporting on is not a report:
    # run from a feature branch against the owner's database, it silently applied that
    # branch's migrations and left the checkout launchd runs unable to start (2026-08-23,
    # tasks/lessons.md).
    #
    # And migrating here made this module lie about itself. `schema.unapplied` and the
    # "schema is behind" branch of its own verdict were both unreachable — the migration
    # ran two lines before the probe that looks for pending ones, so the field was
    # structurally always empty and the verdict could only ever say "current". A state
    # report whose schema section cannot report a pending migration is missing the one
    # thing it exists to catch.
    #
    # An unmigrated ledger is therefore a state this command must survive rather than
    # repair: probes over tables that do not exist yet answer `unknown`, which is the
    # founding rule, and the verdict names the pending migrations and the remedy.
    conn = _open(settings)
    snapshot = state_mod.collect(conn, settings)
    checks = state_mod.verdicts(snapshot, conn, settings)
    failed = [v for v in checks if not v.ok]

    if as_json:
        typer.echo(state_mod.as_json(snapshot, checks))
        raise typer.Exit(1 if failed else 0)

    if not quiet:
        for section, claims in snapshot.as_dict().items():
            typer.echo(section)
            for name, claim in claims.items():
                if claim.get("unknown"):
                    typer.echo(f"  {name}: unknown — {claim['unknown']}")
                else:
                    typer.echo(f"  {name}: {claim['value']}")
                typer.echo(f"      via {claim['how']}")
        typer.echo("")

    for verdict in checks:
        typer.echo(verdict.line())
    unknown = sum(1 for v in failed if v.unknown)
    if failed:
        typer.echo(
            f"\n{len(failed) - unknown} failing, {unknown} unknown, "
            f"{len(checks) - len(failed)} ok"
        )
    else:
        typer.echo(f"\nall {len(checks)} checks pass")
    raise typer.Exit(1 if failed else 0)


@app.command("app-update")
def app_update_command(
    check: Annotated[
        bool, typer.Option("--check", help="Say what would happen; build nothing")
    ] = False,
    now: Annotated[
        bool,
        typer.Option("--now", help="Quit a running app, install, and start it again"),
    ] = False,
) -> None:
    """Rebuild and reinstall the desktop app when the checkout has moved past it.

    The app freezes its Python, templates and CSS at build time, so every merge leaves
    /Applications behind the code until someone rebuilds by hand — and nothing said so
    until `state` learned to hash the frozen surfaces. This is that check on a timer,
    with the rebuild attached.

    It builds only when the bundle demonstrably differs from the checkout, refuses a
    dirty tree, and defers while the app is open. `--now` overrides the last of those.
    """
    from backglass import appupdate

    result = appupdate.run(now=now, check_only=check)
    lead = "would rebuild: " if check and result.decision.build else ""
    typer.echo(lead + result.decision.reason)
    for surface in result.decision.stale[:8]:
        typer.echo(f"  · {surface}")
    if len(result.decision.stale) > 8:
        typer.echo(f"  · … {len(result.decision.stale) - 8} more")
    if result.installed:
        typer.echo(f"installed; previous app kept at {result.backup}")
    for note in result.notes:
        typer.echo(f"  {note}")
    raise typer.Exit(result.exit_code)


@app.command("purge-boundary")
def purge_boundary(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report, delete nothing")] = False,
) -> None:
    """docs/08 D6: purge stored items that a newly added denylist entry now matches."""
    settings = get_settings()
    conn = _open(settings)
    boundary = Boundary.from_settings(settings)
    if not boundary.enforcing:
        typer.echo(
            f"boundary mode is {settings.boundary_mode!r} with "
            f"{len(settings.boundary_deny_domains)} domain(s); nothing to purge"
        )
        return
    report = purge(conn, boundary, dry_run=dry_run)
    verb = "would remove" if dry_run else "removed"
    typer.echo(
        f"{verb} {report.source_items} source item(s), {report.commitments} commitment(s),"
        f" {report.entity_identifiers} stored identifier(s)"
    )
    for rule, count in sorted(report.matched_rules.items()):
        typer.echo(f"  {rule}: {count}")


def _is_loopback(host: str) -> bool:
    """Whether binding `host` keeps the dashboard on this machine.

    `localhost` is accepted by name because it is what the docs and the launchd plists
    say; everything else has to be an address that answers `is_loopback`, which covers
    the whole of 127.0.0.0/8 and ::1 without hard-coding them. An unparseable host is
    not loopback: a hostname that resolves off-machine is exactly the case being caught.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@app.command()
def dashboard(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8765,
    expose_unauthenticated: Annotated[
        bool,
        typer.Option(
            "--expose-unauthenticated",
            help="Bind a non-loopback --host anyway. There is no login: anyone who can "
            "reach this port reads and edits the ledger.",
        ),
    ] = False,
) -> None:
    """Serve the dashboard. docs/06.

    Binds to loopback by default. docs/08: no telemetry leaves the machine, and a page
    rendering the owner's commitments has no business being reachable from the network.

    The dashboard writes as well as reads (docs/06: "if the dashboard is read-only it
    becomes a thing you look at") and has no authentication of any kind, so `--host
    0.0.0.0` hands the whole ledger to the coffee-shop wifi. That is a one-character
    mistake away from the default, so it takes a second flag that says what it costs —
    a guard against the accident, not a substitute for the auth this does not have.
    """
    from backglass.web.app import serve

    if not _is_loopback(host) and not expose_unauthenticated:
        typer.echo(
            f"refusing to bind {host}: the dashboard has no login, so this would publish "
            f"the ledger to everyone on the network. docs/08. Loopback (127.0.0.1) is the "
            f"default; pass --expose-unauthenticated if you meant it.",
            err=True,
        )
        raise typer.Exit(code=1)
    if not _is_loopback(host):
        typer.echo(f"serving unauthenticated on {host} — reachable from the network", err=True)

    settings = get_settings()
    typer.echo(f"http://{host}:{port}  (db: {settings.db_path})")
    serve(settings, host=host, port=port)


@app.command()
def plan(
    for_date: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD")] = None,
    accept: Annotated[
        bool, typer.Option("--accept", help="Mark the proposal accepted")
    ] = False,
    if_missing: Annotated[
        bool,
        typer.Option(
            "--if-missing",
            help="Do nothing if the day already has a live plan (catch-up runs)",
        ),
    ] = False,
    all_overflow: Annotated[
        bool,
        typer.Option("--all", help="List every item that did not fit, not just what is due"),
    ] = False,
) -> None:
    """Propose a day. docs/04 §1.

    A proposal, never an imposition — docs/04 §6 rules out automatic calendar writes, so
    this writes a `day_plan` and nothing else. Regenerating supersedes the prior plan and
    keeps it (§3).

    `--if-missing` makes the run a no-op when the day already has a live plan. That is
    what the login catch-up job (`com.backglass.plan-catchup`) uses: a machine that was
    shut down at 05:45 gets its plan when it is next opened, and a machine that was
    awake keeps the 05:45 plan — including any edits or acceptance — untouched.
    """

    from backglass.goals import health
    from backglass.plan import planner, timezones

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    day = _day_option(for_date, "--date") or _today(settings)

    if if_missing and planner.current_plan_id(conn, day) is not None:
        typer.echo(f"{day}: already planned — nothing to do")
        return

    # The morning ask: refresh the detectors BEFORE proposing, so a conflict that
    # exists this morning is a question this morning — not whenever the owner happens
    # to open /ask. Best-effort (rule 5): a detector down must not cost the plan.
    waiting = 0
    try:
        from backglass import questions as questions_mod

        questions_mod.refresh(conn, settings, day)
        waiting = len(questions_mod.open_questions(conn))
    except Exception:  # noqa: BLE001 — rule 5: degrade, never block
        pass

    proposal = planner.propose(
        conn,
        settings,
        day,
        at_risk_goals=health.at_risk_goal_ids(conn, settings, day),
        # The wall clock, so a run that launchd deferred to the evening plans the evening.
        now=timezones.local_now(settings),
    )
    plan_id = planner.persist(conn, settings, proposal)
    if accept:
        conn.execute(
            "UPDATE day_plan SET status = 'accepted', accepted_at = ? WHERE id = ?",
            (now_iso(), plan_id),
        )

    cap = proposal.capacity
    typer.echo(
        f"{day} ({cap.tz})  capacity {cap.capacity_minutes}m of {cap.window_minutes}m "
        f"(fixed {cap.fixed_minutes}m, buffer {cap.buffer_minutes}m, "
        f"travel {cap.travel_minutes}m, reserve {cap.reserve_minutes}m)"
    )
    for block in proposal.blocks:
        mark = {
            "protected": " [protected]",
            "fixed": " [fixed]",
            "small": " [small]",
            "routine": " [routine]",
        }.get(str(block["kind"]), "")
        start = timezones.t12(block["starts_at"])
        end = timezones.t12(block["ends_at"])
        typer.echo(f"  {start}–{end}  {block['title']}{mark}")
    for note in proposal.notes:
        typer.echo(f"  · {note}")
    if waiting:
        typer.echo(
            f"  · {waiting} question(s) waiting — answer at /ask; "
            "the plan is planned AROUND them until you do"
        )
    _echo_overflow(proposal, all_overflow)


def _echo_overflow(proposal: Any, all_overflow: bool = False) -> None:
    """P2 without the wall of text.

    "Never silently truncate" is the rule, and printing all of them obeyed it the way a
    thirty-page contract obeys disclosure. The owner's 2026-08-09 ended in 46 lines, most
    of them restatements of each other, and a list that long is not read — so the items
    in it that were actually due that day were as invisible as if they had been dropped.

    What did not fit AND is already due or overdue is printed in full: those are the ones
    where not fitting is news. The rest is counted by the band it fell in, so the total
    still adds up in view and nothing is merely gone. `--all` prints every line for when
    the tail itself is the thing being looked at.

    The count line is the planner's own P2 note, already in `proposal.notes` — printing a
    second total here would be two numbers describing one pile.
    """
    from backglass.plan import planner

    if not proposal.overflow:
        return
    # Keyed by the priority each candidate already carries, so this stays a way of reading
    # the planner's ordering rather than a second opinion about it.
    bands = (
        (planner.PRIORITY_OVERDUE, "overdue"),
        (planner.PRIORITY_DUE_TODAY, "due today"),
        (planner.PRIORITY_AT_RISK_GOAL, "for a goal at risk"),
        (planner.PRIORITY_DUE_THIS_WEEK, "due this week"),
        (planner.PRIORITY_REST, "no date"),
    )
    shown = (
        proposal.overflow
        if all_overflow
        else [c for c in proposal.overflow if c.priority <= planner.PRIORITY_DUE_TODAY]
    )
    for item in shown:
        typer.echo(f"  · did not fit: {item.what} ({item.minutes}m)", err=True)
    rest = [c for c in proposal.overflow if c not in shown]
    if not rest:
        return
    counts = [
        (label, sum(1 for c in rest if c.priority == priority)) for priority, label in bands
    ]
    summary = ", ".join(f"{n} {label}" for label, n in counts if n)
    typer.echo(f"  · {len(rest)} more did not fit — {summary}", err=True)


@app.command()
def shutdown(
    for_date: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD")] = None,
    done: Annotated[
        str | None, typer.Option("--done", help="Comma-separated block ids")
    ] = None,
    learned: Annotated[str | None, typer.Option("--learned")] = None,
    blocked: Annotated[str | None, typer.Option("--blocked")] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Close today even though the working window is open"),
    ] = False,
    catch_up: Annotated[
        bool,
        typer.Option("--catch-up", help="Close every past day nobody closed, unbounded"),
    ] = False,
) -> None:
    """The evening pass. docs/04 §1.8.

    Optional and skippable. If no `--done` is given, completion is inferred from ledger
    state and the rest rolls over. Nothing here nags about a skipped shutdown — docs/04:
    "A productivity system that scolds gets deleted."

    Refuses to close today while the working window is still open, unless `--force`. This
    is a pass that decides what did not get done and rolls it into tomorrow; run at 09:30
    it makes that judgement about a day with twelve hours left in it. That is not a
    hypothetical — the launchd job asks at 22:00 and, on a machine whose calendar agent
    still holds the timezone it booted in, fired at 09:30 for weeks (2026-08-17). A
    scheduled job that can be pointed at the wrong hour needs the wrong hour to be
    harmless, and the hour a pass belongs after is something this program already knows.
    """

    from backglass.brief import weekly
    from backglass.plan import rollover, timezones

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    # One clock for both the day and the guard below. `_today` reads `default_tz` while the
    # window is measured in the day's *active* zone, and a guard that compares two clocks
    # is a guard that disagrees with itself the week the owner is in Coimbatore.
    now = timezones.local_now(settings)
    day = _day_option(for_date, "--date") or now.date()

    if catch_up:
        # The one-time counterpart to the net in `catchup.py`, with the fortnight bound
        # lifted. Separate from the ordinary path on purpose: this rewrites rollover
        # counts across history, which is a thing to type deliberately rather than a
        # thing a scheduled job decides to do.
        from backglass import catchup as catchup_mod

        produced = catchup_mod.close_ended_days(conn, settings, day, lookback=None)
        conn.commit()
        for item in produced:
            typer.echo(f"closed {item.day}: {item.detail}")
        # Days whose live plan holds no work because an empty one superseded a real plan
        # cannot be closed from here and must not be: a superseded plan is the day's
        # history, and reaching back into it would be rewriting the record rather than
        # completing it. `planner.persist`'s guard stops new ones being made; these are
        # the ones made before it existed, and they are named rather than fixed.
        stranded = conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT p.local_date FROM day_plan p"
            "  WHERE p.user_id = ? AND p.status != 'superseded' AND p.planned_minutes = 0"
            "    AND p.local_date < ?"
            "    AND EXISTS (SELECT 1 FROM day_plan q JOIN plan_block b"
            "                  ON b.day_plan_id = q.id"
            "                WHERE q.user_id = p.user_id AND q.local_date = p.local_date"
            "                  AND q.status = 'superseded' AND b.kind IN ('work',"
            "                      'protected', 'small'))"
            "  GROUP BY p.local_date)",
            (USER_ID, day.isoformat()),
        ).fetchone()
        if stranded and int(stranded["n"]):
            typer.echo(
                f"{stranded['n']} day(s) hold no closeable work because an empty plan"
                " superseded a real one — left as they are; their history is in the"
                " superseded rows"
            )
        if not produced:
            typer.echo("nothing to catch up — every past day is closed")
        return

    # Only today can be premature; a `--date` in the past is a day that is genuinely over,
    # and the evening pass on a finished day is the ordinary catch-up case.
    _, window_end = timezones.window_on(settings, day)
    if not force and day == now.date() and now < window_end:
        # Exit 0: this is a designed skip, not a failure. A non-zero exit would turn every
        # misfire into a red line in a log nobody reads, which is how a real failure gets
        # missed. Same reasoning as the `SyncLocked` skip in `sync`.
        typer.echo(
            f"{day}: the working window is open until {window_end.strftime('%H:%M')} — "
            f"not closing the day at {now.strftime('%H:%M')}. Pass --force to override."
        )
        return

    ids = {int(part) for part in done.split(",") if part.strip()} if done else None
    report = rollover.close_day(conn, settings, day, done_block_ids=ids)
    if learned or blocked:
        rollover.record_shutdown(conn, day, learned=learned, blocked=blocked)
    made = weekly.link_completed_work(conn)

    dropped = f", {report.dropped} dropped" if report.dropped else ""
    typer.echo(
        f"{day}: {report.done} done, {report.rolled} rolled{dropped},"
        f" {made} checkpoint(s)"
    )
    for row in report.flagged:
        typer.echo(f"  · rolled {row['rollover_count']}x: {row['what']}")


def _today(settings: Settings):  # type: ignore[no-untyped-def]
    from backglass.brief.daily import today_in

    return today_in(settings.default_tz)


def _day_option(value: str | None, flag: str):  # type: ignore[no-untyped-def]
    """Read a `YYYY-MM-DD` command-line option, or exit with a sentence about it.

    `date.fromisoformat` fails in two ways — a shape it cannot read (`ValueError:
    Invalid isoformat string`) and a shape it reads into a day that does not exist
    (`ValueError: day is out of range for month`) — and every CLI option that took a
    date let both out as a traceback. One of them (`log --on`) had a message; the rest
    printed forty lines of Rich frames and the word ValueError, which tells the owner
    they broke the program rather than that they mistyped a date.

    Returns None for None so `--date` staying unset still means "today", which is each
    caller's own default and not this function's business.
    """
    from datetime import date as _date

    if value is None:
        return None
    try:
        return _date.fromisoformat(value)
    except ValueError:
        typer.echo(f"{flag} {value!r} is not a date; use YYYY-MM-DD", err=True)
        raise typer.Exit(code=1) from None


@app.command()
def brief(
    send: Annotated[bool, typer.Option("--send", help="Deliver it; otherwise print")] = False,
    for_date: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD")] = None,
    html_out: Annotated[
        Path | None, typer.Option("--html", help="Write the email HTML to a file")
    ] = None,
) -> None:
    """Generate the morning brief. docs/05.

    B6: a generation failure produces a short failure notice rather than silence, because
    "silence is indistinguishable from 'no news' and that ambiguity is corrosive".
    """

    from backglass.brief import daily, deliver, render

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    target = _day_option(for_date, "--date") or daily.today_in(settings.default_tz)

    degraded = False
    try:
        built = daily.build_for(conn, settings, target)
        degraded = any(section.priority == 0 for section in built.sections)
    except Exception as exc:  # noqa: BLE001 - B6: never fail silently
        typer.echo(f"generation failed: {type(exc).__name__}: {exc}", err=True)
        built = daily.failure_brief(target, f"{type(exc).__name__}")
        degraded = True

    base = settings.dashboard_base_url
    brief_id = daily.persist(conn, built, render.to_markdown(built, base_url=base))
    html = render.to_html(built, base_url=base, brief_id=brief_id)

    if html_out is not None:
        html_out.write_text(html)
        typer.echo(f"wrote {html_out}")

    if not send:
        typer.echo(render.to_text(built, base_url=base))
        typer.echo(f"\n[{built.word_count()} words, limit {model.WORD_LIMIT}; not sent]")
        return

    if not settings.brief_to.strip():
        # Email never configured is a choice, not a failure: the brief above is already
        # persisted and readable at /brief, and doctor's "brief recipient configured"
        # check is where the absence is surfaced. Exiting 1 here made the scheduled job
        # log the same non-error every morning forever. A *partial* delivery config
        # (BRIEF_TO set, key or sender missing) still falls through to DeliveryError —
        # someone who named a recipient wants the mail to arrive.
        typer.echo(
            "not emailed: BRIEF_TO is not set — the brief is saved and readable at "
            f"{base}/brief"
        )
        return

    try:
        result = deliver.Sender(settings).send(
            subject=deliver.subject_for(built.generated_for_date, degraded=degraded),
            html=html,
            text=render.to_text(built, base_url=base),
        )
    except deliver.DeliveryError as exc:
        typer.echo(f"delivery failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    conn.execute("UPDATE brief SET sent_at = ? WHERE id = ?", (now_iso(), brief_id))
    typer.echo(f"sent {result.detail} to {settings.brief_to} ({built.word_count()} words)")


# ──────────────────────────────────────────────────────────────── helpers


def _google_service(
    conn: sqlite3.Connection,
    settings: Settings,
    source: str,
    api: str,
    version: str,
    scopes: list[str],
) -> Any | None:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    record = credentials.load(conn, source)
    if record is None or not record.refresh_token:
        typer.echo(f"{source} is not authorised; skipping", err=True)
        return None
    creds = Credentials(  # type: ignore[no-untyped-call]
        token=record.access_token,
        refresh_token=record.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=scopes,
    )
    return build(api, version, credentials=creds, cache_discovery=False)


def _contacts_source(conn: sqlite3.Connection, settings: Settings) -> Any:
    """The address book, or None. Kept out of `_all_connectors` on purpose.

    It is not a `Connector`: no cursor, no `SourceItem`, nothing written to the ledger's
    capture table. Putting it in the connector list would have it fetched, cursored and
    counted as ingest, all of which are wrong for reference data — see
    backglass/contacts.py.

    It takes `conn` for the same reason `_all_connectors` does, and this is the important
    half: config says what CAN run, the credential row says what DOES. Being outside the
    connector list is not a reason to be outside the pause switch — it is the reason this
    line has to exist separately, and skipping it made `sources disable apple-contacts`
    print a confirmation, set `enabled = 0`, render a paused square with a Resume button,
    and then read the owner's entire address book on the next sync anyway. An inert
    privacy control that reports success is worse than no control.
    """
    from backglass.connectors.contacts import SOURCE, ContactsSource

    if not settings.apple_contacts:
        return None
    if SOURCE in credentials.disabled_sources(conn):
        return None
    return ContactsSource(boundary=Boundary.from_settings(settings))


def _all_connectors(conn: sqlite3.Connection, settings: Settings) -> list[Connector]:
    """Every configured source. docs/02 §Failure policy: one failing source does not stop
    the others, so an unauthorised connector is skipped rather than fatal."""
    from backglass import chats as chats_mod
    from backglass.connectors.calendar import SCOPES as CAL_SCOPES
    from backglass.connectors.calendar import CalendarConnector
    from backglass.connectors.canvas import CanvasConnector
    from backglass.connectors.drive import SCOPES as DRIVE_SCOPES
    from backglass.connectors.drive import DriveConnector
    from backglass.connectors.notes import NotesConnector

    boundary = Boundary.from_settings(settings)
    built: list[Connector] = []

    for label in settings.gmail_accounts:
        service = _google_service(conn, settings, f"gmail:{label}", "gmail", "v1", SCOPES)
        if service is not None:
            built.append(GmailConnector(label=label, service=service, boundary=boundary))

    for label in settings.calendar_accounts:
        service = _google_service(
            conn, settings, f"calendar:{label}", "calendar", "v3", CAL_SCOPES
        )
        if service is not None:
            built.append(CalendarConnector(label=label, service=service, boundary=boundary))

    for label in settings.drive_accounts:
        service = _google_service(conn, settings, f"drive:{label}", "drive", "v3", DRIVE_SCOPES)
        if service is not None:
            built.append(
                DriveConnector(
                    label=label,
                    service=service,
                    boundary=boundary,
                    owner_emails=tuple(settings.owner_emails),
                )
            )

    if settings.obsidian_vault_path:
        built.append(NotesConnector(vault_path=settings.obsidian_vault_path, boundary=boundary))

    if settings.canvas_base_url and settings.canvas_token:
        built.append(
            CanvasConnector(
                base_url=settings.canvas_base_url,
                token=settings.canvas_token,
                boundary=boundary,
            )
        )
    elif settings.canvas_ics_url:
        # `elif`, deliberately. The two read the same assignments and would write them
        # twice under two source names, and the API path is strictly better — it knows
        # what has been submitted. So the token wins whenever there is one, and the feed
        # is what an institution's token policy leaves behind.
        from backglass.connectors.canvas_ics import CanvasIcsConnector

        built.append(
            CanvasIcsConnector(feed_url=settings.canvas_ics_url, boundary=boundary)
        )

    # ── Phase 7 sources — same protocol, config-gated like everything above ──
    if settings.github_token:
        from backglass.connectors.github import GithubConnector

        built.append(GithubConnector(token=settings.github_token, boundary=boundary))

    if settings.slack_token and settings.slack_channels:
        from backglass.connectors.slack import SlackConnector

        built.append(
            SlackConnector(
                token=settings.slack_token,
                channel_ids=tuple(settings.slack_channels),
                boundary=boundary,
            )
        )

    if settings.imessage_db_path:
        from backglass.connectors.imessage import IMessageConnector

        # Migration path, run before the allowlist is read: whatever is still named in
        # `.env` becomes a `monitor` row, so a machine configured the old way keeps
        # working and the page shows those chats as already decided.
        chats_mod.seed_from_env(conn, "imessage", settings.imessage_chats)

        built.append(
            IMessageConnector(
                db_path=settings.imessage_db_path,
                boundary=boundary,
                # From the table, not the setting. `.env` entries are seeded in on the
                # first run so an existing configuration keeps working, after which the
                # decisions the owner made on the page are the only thing that matters.
                allowlist=chats_mod.allowlist_for(
                    conn, "imessage", settings.imessage_chats
                ),
                lookback_days=settings.imessage_lookback_days,
            )
        )

    if settings.instagram_export_path or (
        settings.instagram_username and settings.instagram_session_file
    ):
        from backglass.connectors.instagram import (
            InstagramExportConnector,
            InstagramLiveConnector,
        )

        # Same shape as iMessage above: `.env` seeds the table once, the table decides
        # from then on — and an empty allowlist no longer disables the connectors,
        # because a run with nothing chosen is what discovers the chats to choose from.
        chats_mod.seed_from_env(conn, "instagram", settings.instagram_chats)
        allowlist = chats_mod.allowlist_for(conn, "instagram", settings.instagram_chats)
        if settings.instagram_export_path:
            built.append(
                InstagramExportConnector(
                    export_path=settings.instagram_export_path,
                    allowlist=allowlist,
                    boundary=boundary,
                )
            )
        if settings.instagram_username and settings.instagram_session_file:
            built.append(
                InstagramLiveConnector(
                    username=settings.instagram_username,
                    session_file=settings.instagram_session_file,
                    allowlist=allowlist,
                    boundary=boundary,
                )
            )

    if settings.inbox_folder_path:
        from backglass.connectors.files import FilesConnector

        built.append(
            FilesConnector(folder_path=settings.inbox_folder_path, boundary=boundary)
        )

    if settings.apple_notes:
        from backglass.connectors.apple_notes import AppleNotesConnector

        built.append(AppleNotesConnector(boundary=boundary))

    if settings.apple_reminders:
        from backglass.connectors.reminders import RemindersConnector

        built.append(RemindersConnector(boundary=boundary))

    if settings.apple_calendar:
        from backglass.connectors.apple_calendar import AppleCalendarConnector

        built.append(
            AppleCalendarConnector(
                boundary=boundary, skip=tuple(settings.apple_calendar_skip)
            )
        )

    if settings.apple_mail_path:
        from backglass.connectors.apple_mail import AppleMailConnector

        built.append(
            AppleMailConnector(
                mail_root=settings.apple_mail_path,
                boundary=boundary,
                lookback_days=settings.apple_mail_lookback_days,
                out_of_scope_accounts=frozenset(
                    a.strip().lower() for a in settings.boundary_out_of_scope_accounts
                ),
            )
        )

    # ── Spaced-repetition sources. No boundary: review
    # tallies carry no addresses and no card content is ingested. ──
    if settings.anki_db_path or settings.avorio_db_path:
        from datetime import date as _date

        from backglass.plan import timezones

        tz = timezones.active_tz(settings, _date.today())
        if settings.anki_db_path:
            from backglass.connectors.anki import AnkiConnector

            built.append(AnkiConnector(db_path=settings.anki_db_path, tz=tz))
        if settings.avorio_db_path:
            from backglass.connectors.avorio import AvorioConnector

            built.append(AvorioConnector(db_path=settings.avorio_db_path, tz=tz))

    # The pause switch: a disabled source stays configured and keeps its cursor, but
    # never fetches. Config says what CAN run; the credential row says what DOES.
    disabled = credentials.disabled_sources(conn)
    return [c for c in built if c.name not in disabled]


def _print_report(report: Any, *, dry_run: bool) -> None:
    head = "dry run — nothing written" if dry_run else f"run at {now_iso()}"
    typer.echo(head)
    typer.echo(
        f"  fetched {report.fetched}, excluded {report.excluded}, "
        f"rules dropped {report.rule_dropped}, model triaged {report.model_triaged}, "
        f"extracted {report.extracted}, parked {report.parked}"
    )
    typer.echo(
        f"  commitments +{report.commitments_inserted} "
        f"(deduped {report.commitments_deduped}, superseded {report.commitments_superseded}, "
        f"review queue {report.review_queue})"
    )
    if getattr(report, "contacts_linked", 0):
        typer.echo(f"  contacts linked {report.contacts_linked} identifier(s) to people")
    if getattr(report, "retracted", 0):
        # Said out loud, because it changes the day's capacity. The owner's schedule
        # changed on 2026-08-10 and five dropped classes went on being planned around for
        # ten days with nothing anywhere reporting it; a retraction that only ever shows
        # up as an absence would repeat exactly that.
        typer.echo(
            f"  retracted {report.retracted} item(s) their source no longer has — "
            "your calendar changed"
        )
    # Said on stdout rather than stderr, and above the write count, because these are the
    # two things a Canvas re-read is *for*. Both were being computed and thrown away: the
    # revision had nowhere to go but the error list, and the due-date note nowhere at all.
    for revision in getattr(report, "upstream_revisions", []):
        typer.echo(f"  upstream: {revision}")
    for note in getattr(report, "coursework_notes", []):
        typer.echo(f"  coursework: {note}")
    # Said out loud for the same reason, and a stronger one: a goal link changes what the
    # planner promotes tomorrow. A reprioritised day whose cause appears nowhere in the
    # run's own output is the visibility rule failing at the last step.
    for note in getattr(report, "goal_link_notes", []):
        typer.echo(f"  goal: {note}")
    typer.echo(f"  writes {report.writes}, spend {report.spend_cents}c")
    # startswith, because the reason carries which stage stopped ('rate_limit:triage').
    if (report.degrade_reason or "").startswith("rate_limit"):
        stage = str(report.degrade_reason).partition(":")[2]
        typer.echo(
            f"  DEGRADED: model rate limit reached{f' during {stage}' if stage else ''}; "
            "the remaining items are still pending and the next sync retries them",
            err=True,
        )
    elif report.degraded:
        typer.echo("  DEGRADED: spend cap reached, extraction skipped", err=True)
    if getattr(report, "model_auth_failed", False):
        # Deliberately not folded into the error list below: those read as "this item
        # went wrong", and the whole point is that these items never went anywhere.
        typer.echo(
            "  MODEL AUTH FAILED: the backend rejected our credentials — the items "
            "below were not read, not judged. Re-authenticate the model backend "
            "(claude_cli: run `claude` once interactively to refresh the session).",
            err=True,
        )
    for rule, count in sorted(report.excluded_by_rule.items()):
        typer.echo(f"  boundary excluded {count} by rule {rule}")
    for note in report.date_notes:
        typer.echo(f"  date: {note}", err=True)
    for error in report.errors:
        typer.echo(f"  error: {error}", err=True)


def main() -> None:
    """The console-script entry point.

    The backstop for a refused run. `sync`, `batch submit` and `batch collect` each
    catch `SyncLocked` and print their own sentence, which is better than a generic one
    — but a lock is not an error in the program, it is the program correctly declining
    to be the second writer, and the next command to take the lock would otherwise
    traceback until someone remembered to add a fourth handler. This one costs three
    lines and cannot be forgotten.
    """
    try:
        sys.exit(app())
    except SyncLocked as exc:
        typer.echo(str(exc), err=True)
        sys.exit(1)


# ── Phase 6: people ───────────────────────────────────────────────────────

people_app = typer.Typer(
    invoke_without_command=True,
    help="Networking profiles over the entity ledger.",
)
app.add_typer(people_app, name="people")


@people_app.callback()
def people_list(
    ctx: typer.Context,
    search: Annotated[str | None, typer.Option("--search", "-s")] = None,
    tag: Annotated[str | None, typer.Option("--tag")] = None,
    cold: Annotated[bool, typer.Option("--cold", help="Only going-cold profiles")] = False,
) -> None:
    """List profiles. Same query the People page runs."""
    if ctx.invoked_subcommand is not None:
        return
    from backglass.people import profiles, touch

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    touches = {t.entity_id: t for t in touch.cold(conn, settings, _today(settings))}
    rows = profiles.search(conn, q=search, tag=tag)
    if cold:
        rows = [r for r in rows if touches.get(r["id"]) and touches[r["id"]].level != "fresh"]
    if not rows:
        typer.echo("no profiles match")
        raise typer.Exit()
    for r in rows:
        t = touches.get(r["id"])
        bits = [f"[{r['id']}]", r["canonical_name"]]
        if r["role"]:
            bits.append(r["role"])
        if r["org"]:
            bits.append(r["org"])
        if r["tags"]:
            bits.append(",".join(r["tags"]))
        if r["open_count"]:
            bits.append(f"{r['open_count']} open")
        if t and t.level != "fresh":
            bits.append(t.chip())
        typer.echo("  ".join(bits))


@people_app.command("show")
def people_show(entity_id: int) -> None:
    """One profile: open commitments both directions, then the timeline."""
    from backglass.people import profiles

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    record = profiles.profile(conn, entity_id)
    if record is None:
        typer.echo(f"no entity {entity_id}", err=True)
        raise typer.Exit(code=1)
    line = record["canonical_name"]
    extras = " · ".join(x for x in (record["role"], record["org"]) if x)
    typer.echo(f"{line}{' — ' + extras if extras else ''}")
    if record["aliases"]:
        typer.echo(f"known as: {', '.join(record['aliases'])}")
    # The notes are the part a person actually wrote by hand, and this command printed
    # everything except them — a profile carrying a career synopsis showed one line of
    # role and org, and the only way to read the rest was the web page.
    if record["notes"]:
        typer.echo()
        for paragraph in str(record["notes"]).split("\n"):
            typer.echo(f"  {paragraph}" if paragraph.strip() else "")
        typer.echo()
    for c in profiles.open_commitments(conn, entity_id):
        arrow = "you owe" if c["direction"] == "i_owe" else "owed to you"
        due = f" due {c['due_at'][:10]}" if c["due_at"] else ""
        typer.echo(f"  {arrow}: {c['what']}{due}")
    for item in profiles.timeline(conn, entity_id)[:10]:
        typer.echo(
            f"  {str(item['source_occurred_at'])[:10]}  {item['source_title'] or 'no subject'}"
            f"  ({item['source']})"
        )


@people_app.command("edit")
def people_edit(
    entity_id: int,
    role: Annotated[str | None, typer.Option("--role")] = None,
    org: Annotated[str | None, typer.Option("--org")] = None,
    tags: Annotated[str | None, typer.Option("--tags", help="comma-separated")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Set role/org/tags/notes. Unset options keep their current value."""
    from backglass.people import profiles
    from backglass.web import actions

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    current = profiles.profile(conn, entity_id)
    if current is None:
        typer.echo(f"no entity {entity_id}", err=True)
        raise typer.Exit(code=1)
    actions.person_update(
        conn,
        entity_id,
        role=role if role is not None else current["role"],
        org=org if org is not None else current["org"],
        tags=tags if tags is not None else ",".join(current["tags"]),
        notes=note if note is not None else current["notes"],
    )
    typer.echo("updated")


@people_app.command("merge")
def people_merge(winner_id: int, loser_id: int) -> None:
    """Fold LOSER into WINNER: aliases union, commitments repoint, audit row kept."""
    from backglass.people import merge as merge_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    try:
        report = merge_mod.merge(conn, winner_id, loser_id)
    except merge_mod.MergeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"merged into {report['winner_id']}: {report['commitments_repointed']} commitment(s)"
        f" repointed, aliases now {', '.join(report['aliases'])}"
    )


@app.command("add")
def add_commitment(
    what: str,
    owe: Annotated[bool, typer.Option("--owe", help="You owe this")] = False,
    owed: Annotated[bool, typer.Option("--owed", help="Owed to you")] = False,
    who: Annotated[str | None, typer.Option("--who")] = None,
    due: Annotated[str | None, typer.Option("--due", help="YYYY-MM-DD")] = None,
    minutes: Annotated[int | None, typer.Option("--minutes")] = None,
    goal: Annotated[
        int | None,
        typer.Option("--goal", help="Goal id this commitment serves (see `goals link`)"),
    ] = None,
) -> None:
    """Quick-add a commitment by hand. Provenance is a manual source item.

    `--goal` is the same link `goals link` makes, at the moment the commitment is typed:
    docs/04 §2 wants commitments read against goals, and a link nobody can set at entry
    time is a link nobody sets at all.
    """
    from backglass.goals import checkpoints
    from backglass.web import actions

    if owe == owed:
        typer.echo("pass exactly one of --owe / --owed", err=True)
        raise typer.Exit(code=1)
    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    # Checked before the insert, not after: the connection is autocommit, so linking a
    # bad goal id afterwards would leave the commitment written and unlinked.
    goal_title = _goal_title(conn, goal) if goal is not None else None
    if goal is not None and goal_title is None:
        typer.echo(f"no goal {goal}", err=True)
        raise typer.Exit(code=1)
    cid = actions.quick_add(
        conn,
        settings,
        what=what,
        direction="i_owe" if owe else "owed_to_me",
        counterparty=who,
        due_at=due,
        minutes=minutes,
    )
    if goal is not None:
        checkpoints.link_commitment(conn, cid, goal)
        typer.echo(f"commitment {cid} added, linked to goal {goal}: {goal_title}")
        return
    typer.echo(f"commitment {cid} added")


# ── Phase 6: roadmaps ─────────────────────────────────────────────────────

roadmap_app = typer.Typer(help="Preset career-path roadmaps over the goal engine.")
app.add_typer(roadmap_app, name="roadmap")

goals_app = typer.Typer(help="Goal targets beyond what the dashboard edits.")
app.add_typer(goals_app, name="goals")


def _goal_title(conn: sqlite3.Connection, goal_id: int) -> str | None:
    row = conn.execute(
        "SELECT title FROM goal WHERE id = ? AND user_id = ?", (goal_id, USER_ID)
    ).fetchone()
    return str(row["title"]) if row is not None else None


@goals_app.command("link")
def goals_link(
    commitment_id: Annotated[int, typer.Argument(help="Commitment to point at a goal")],
    goal_id: Annotated[int, typer.Argument(help="The goal it serves")],
) -> None:
    """Point a commitment at the goal it serves — docs/04's "commitments against goals".

    Everything downstream of `commitment.goal_id` already reads it: the planner ranks a
    goal-linked commitment ahead of an unlinked one, closing a linked commitment records
    a checkpoint, the weekly retro groups the week's work by goal, and the board card
    shows the goal's title. None of it fires until something writes the link, and until
    this command there was no way to.
    """
    from backglass.goals import checkpoints

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    row = conn.execute(
        "SELECT what FROM commitment WHERE id = ? AND user_id = ?",
        (commitment_id, USER_ID),
    ).fetchone()
    if row is None:
        typer.echo(f"no commitment {commitment_id}", err=True)
        raise typer.Exit(code=1)
    try:
        checkpoints.link_commitment(conn, commitment_id, goal_id)
    except checkpoints.CheckpointError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    conn.commit()
    typer.echo(
        f"commitment {commitment_id} ({row['what']}) → goal {goal_id}: "
        f"{_goal_title(conn, goal_id)}"
    )


@goals_app.command("add-total")
def goals_add_total(
    goal_id: int,
    title: str,
    total: Annotated[int, typer.Argument(help="The lifetime number to reach")],
) -> None:
    """Attach a lifetime accumulator to any goal (Phase 10): hours, users, interviews.

    Progress is logged as checkpoints (delta = amount) from the roadmap page or
    `record()`; the bar renders wherever the goal's targets do.
    """
    from backglass.roadmap import instantiate

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    if conn.execute(
        "SELECT 1 FROM goal WHERE id = ? AND user_id = 1", (goal_id,)
    ).fetchone() is None:
        typer.echo(f"no goal {goal_id}", err=True)
        raise typer.Exit(code=1)
    try:
        target_id = instantiate.add_total(conn, goal_id, title, total)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    conn.commit()
    typer.echo(f"target {target_id}: {title} — 0/{total}")


@goals_app.command("add-periodic")
def goals_add_periodic(
    goal_id: int,
    title: str,
    every_days: Annotated[
        int, typer.Argument(help="Cadence in days — 182 is roughly every six months")
    ],
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="YYYY-MM-DD it was last done; defaults to today, so it is not "
            "instantly overdue",
        ),
    ] = None,
) -> None:
    """Attach a repeating obligation to a goal: advising, an application cycle, a renewal.

    The class of thing nothing upstream can tell you about. A deadline in an email
    becomes a commitment on its own; "it has been six months since you saw your advisor"
    is generated by a clock or by nobody. Due at the cadence, overdue at twice it, and
    the daily brief says so with the day count.

        backglass goals add-periodic 1 "Academic advising check-in" 182
        backglass goals did 47 --on 2026-03-04
    """
    from backglass.roadmap import instantiate

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    if conn.execute(
        "SELECT 1 FROM goal WHERE id = ? AND user_id = ?", (goal_id, USER_ID)
    ).fetchone() is None:
        typer.echo(f"no goal {goal_id}", err=True)
        raise typer.Exit(code=1)
    anchor = _anchor_iso(settings, since)
    try:
        target_id = instantiate.add_periodic(
            conn, goal_id, title, every_days, created_at=anchor
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    conn.commit()
    typer.echo(f"target {target_id}: {title} — every {every_days} days, from {anchor[:10]}")


@goals_app.command("did")
def goals_did(
    target_id: Annotated[int, typer.Argument(help="The periodic target you just did")],
    on: Annotated[
        str | None, typer.Option("--on", help="YYYY-MM-DD; defaults to today")
    ] = None,
    note: Annotated[str | None, typer.Option("--note", help="What happened")] = None,
) -> None:
    """Reset a periodic target's clock. The checkpoint is the reset — G3 and G10 hold.

    Nothing stores a due date. `targets.progress` reads the newest checkpoint on every
    call, so deleting this one moves the target straight back to overdue, which is what
    G10 asks for and what a stored next-due column could never give.
    """
    from backglass.goals import checkpoints

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    row = conn.execute(
        "SELECT t.title, t.kind, t.every_days, g.title AS goal_title "
        "FROM target t JOIN goal g ON g.id = t.goal_id "
        "WHERE t.id = ? AND g.user_id = ?",
        (target_id, USER_ID),
    ).fetchone()
    if row is None:
        typer.echo(f"no target {target_id}", err=True)
        raise typer.Exit(code=1)
    if row["kind"] != "periodic":
        # Every id in this codebase is a bare int and the tables overlap in range, so a
        # wrong-table id validates and writes to the wrong row. Print what is about to be
        # touched and refuse the kinds this command does not mean (2026-08-12).
        typer.echo(
            f"target {target_id} ({row['title']}) is a {row['kind']} target, not periodic; "
            "use `backglass log` or the roadmap page",
            err=True,
        )
        raise typer.Exit(code=1)
    when = _anchor_iso(settings, on)
    checkpoints.record(conn, target_id, source="manual", occurred_at=when, note=note)
    conn.commit()
    typer.echo(
        f"{row['goal_title']} — {row['title']}: done {when[:10]}, "
        f"next in {row['every_days']} days"
    )


def _anchor_iso(settings: Settings, value: str | None) -> str:
    """A day the owner typed, as an instant. Validated here, never at the column.

    `quick_add` once stored "tomorrow" as eight letters in a column its readers sort by.
    A bare date becomes local noon — the honest instant inside a day (goals/reviews) —
    so a Phoenix evening and a Kolkata morning agree on which day it was.
    """
    from datetime import date, datetime

    from backglass.plan import timezones

    raw = (value or "").strip()
    if not raw:
        return timezones.local_now_iso(settings)
    try:
        if len(raw) == 10:
            return timezones.local_noon_iso(settings, date.fromisoformat(raw))
        datetime.fromisoformat(raw)
    except ValueError as exc:
        typer.echo(f"{raw[:40]!r} is not a date; use YYYY-MM-DD", err=True)
        raise typer.Exit(code=1) from exc
    return raw


def _check_reconciles(
    conn: sqlite3.Connection,
    connector: Any,
    check: Callable[[str, bool, str], None],
) -> None:
    """Does the ledger hold everything this connector's store holds?

    The liveness question two rounds of audit could not answer honestly. A credential
    reading `ok` proves the last run did not raise; the age of the newest row proves
    nothing at all, and a threshold over it called a healthy `apple-notes` parked because
    the owner had not written a note in six weeks. `upstream_count` replaces the guess
    with a count, and a count needs no calibration: 65 against 65 is health, 65 against
    40 is a cursor parked past its data.

    It lives in `doctor` rather than in `heartbeat` on purpose. Heartbeat's contract is a
    small pure read that the dashboard and the brief both derive from, and this runs an
    osascript or an HTTPS fetch — putting it there would put a subprocess in every page
    render. `doctor` is where the live probes already are and where someone goes when
    they suspect something.

    Connectors that cannot answer are skipped in silence rather than reported as zero:
    most sources are windowed or paged, `None` means unknown, and a check that prints a
    line for every source it cannot check is a check nobody finishes reading.
    """
    count = getattr(connector, "upstream_count", None)
    if count is None:
        return
    upstream = count()
    if upstream is None:
        return
    stored = conn.execute(
        "SELECT COUNT(*) AS n FROM source_item WHERE user_id = ? AND source = ?",
        (USER_ID, connector.name),
    ).fetchone()["n"]
    check(
        f"{connector.name} fully ingested",
        stored >= upstream,
        f"store has {upstream}, ledger has {stored} — {upstream - stored} never ingested; "
        "the cursor may be parked ahead of the data "
        f"(clear it: UPDATE credential SET cursor = NULL WHERE source = '{connector.name}')",
    )


@app.command("recheck")
def recheck_command(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the verdicts, write nothing")
    ] = False,
    chat: Annotated[
        str | None, typer.Option("--chat", help="One conversation, by key or display name")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable")] = False,
) -> None:
    """Re-read chat commitments against what the conversation said next.

    84 open commitments came from `imessage` across 16 conversations and not one had ever
    closed from a later message in the same chat. That is structural: closure only ever
    fired forward, when a message announced it completed an earlier promise, and friends
    do not talk that way — "bring dress shoes" is answered by bringing dress shoes.

    Runs inside `sync` on every pass. This command is for looking at it: `--dry-run`
    prints what it would do and writes nothing, which is how the first live pass should
    always be read.
    """
    import json as _json

    from backglass.extract import prompts
    from backglass.extract import recheck as recheck_mod
    from backglass.telemetry import Metered

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    calls: list[Any] = []
    report = recheck_mod.run(
        conn,
        settings,
        Metered(_build_model_client(settings), "recheck", calls),
        prompt=prompts.load("recheck-commitments"),
        dry_run=dry_run,
        only=chat,
    )
    if not dry_run:
        conn.commit()

    if as_json:
        typer.echo(_json.dumps({
            "checked": report.checked, "applied": report.applied,
            "pending": report.pending, "discarded": report.discarded,
            "cost_usd": round(report.cost_usd, 4), "errors": report.errors,
        }, indent=2))
        raise typer.Exit(1 if report.errors else 0)

    if not report.checked:
        typer.echo("no conversation has anything new to re-read")
        return
    typer.echo(f"re-read {len(report.checked)} conversation(s): {', '.join(report.checked)}")
    typer.echo(
        f"  {report.applied} closed, {report.pending} waiting on review, "
        f"{report.discarded} discarded (uncited or unverifiable)"
    )
    typer.echo(f"  spend {round(report.cost_usd * 100)}c")
    if dry_run:
        typer.echo("  nothing written")
    for error in report.errors:
        typer.echo(f"  {error}", err=True)
    raise typer.Exit(1 if report.errors else 0)


@app.command("logic")
def logic_command(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print what it would dispose of, write nothing")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable")] = False,
) -> None:
    """Throw out what the record already contradicts.

    Not a judgement call and not a guess: every rule points at the row that disagrees with
    the row it closes — an obligation whose own text reports it done, a question about a
    day that has ended, a question about a commitment that is no longer open. Silence
    never closes anything here; that is `staleness`, and it asks.

    Runs inside `sync`. This command is for looking at it, and `--dry-run` is how the
    first pass on a real ledger should be read. Everything it does is reversible: dropped
    rows are tombstoned, resolved rows keep the rule in `resolution_note`, and every
    disposal is a `decision` you can read back with `backglass decisions`.
    """
    import json as _json

    from backglass import logic as logic_mod
    from backglass.plan import timezones as tz_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    report = logic_mod.run(
        conn, settings, tz_mod.local_now(settings).date(), dry_run=dry_run
    )
    if not dry_run:
        conn.commit()

    if as_json:
        typer.echo(_json.dumps({
            "applied": report.applied,
            "by_rule": report.by_rule(),
            "disposals": [
                {
                    "kind": d.kind, "subject_id": d.subject_id, "rule": d.rule,
                    "action": d.action, "reason": d.reason,
                }
                for d in report.disposals
            ],
            "errors": report.errors,
        }, indent=2))
        raise typer.Exit(1 if report.errors else 0)

    if not report.disposals:
        typer.echo("nothing the record contradicts")
    for disposal in report.disposals:
        typer.echo(f"  {disposal.line()}")
    if report.disposals:
        typer.echo(
            f"{report.applied} disposed of: "
            + ", ".join(f"{rule} ×{n}" for rule, n in report.by_rule().items())
        )
    if dry_run:
        typer.echo("  nothing written")
    for error in report.errors:
        typer.echo(f"  {error}", err=True)
    raise typer.Exit(1 if report.errors else 0)


@app.command("coursework")
def coursework_command(
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Re-read the Canvas feed before printing"),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="With --refresh: print, write nothing")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable")] = False,
) -> None:
    """How long each assignment takes, and what you need open to do it.

    The estimate comes from the assignment itself wherever the assignment says: a runtime
    in the title, a word count, a chapter range. `basis` is which of those it read, and
    `type:` means nothing was stated and the `coursework_defaults` table answered instead.
    Materials are what the text names — a browser an exam will not run without, the
    chapters it covers, the guide it links to.

    `--refresh` re-reads the feed without ingesting it, which is how a due date that moved
    upstream shows up between syncs. `sync` does the same work on its own schedule.
    """
    import json as _json

    from backglass import coursework as coursework_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)

    if refresh:
        connectors = [
            c for c in _all_connectors(conn, settings) if c.name.startswith("canvas")
        ]
        if not connectors:
            typer.echo("no Canvas source is configured", err=True)
            raise typer.Exit(1)
        for connector in connectors:
            # The items are discarded on purpose: recording them is `sync`'s job and its
            # ledger is the only thing allowed to decide what a new source item means.
            # This read is here for the assignments the fetch parses along the way.
            for _ in connector.fetch(None):
                pass
            report = coursework_mod.upsert(
                conn,
                settings,
                connector.name,
                list(getattr(connector, "assignments", [])),
                dry_run=dry_run,
            )
            for note in report.notes:
                typer.echo(f"  {note}")
            typer.echo(
                f"{connector.name}: {report.inserted} new, {report.updated} changed, "
                f"{report.unchanged} unchanged; materials +{report.materials_added} "
                f"-{report.materials_removed}"
            )
        if not dry_run:
            applied, notes = coursework_mod.apply_estimates(conn)
            for note in notes:
                typer.echo(f"  {note}")
            typer.echo(f"{applied} commitment estimate(s) updated")
            conn.commit()
        else:
            typer.echo("  nothing written")

    rows = coursework_mod.rows(conn)
    if as_json:
        typer.echo(
            _json.dumps(
                [
                    {
                        "id": r.id,
                        "course": r.course,
                        "title": r.title,
                        "due_at": r.due_at,
                        "minutes": r.effort_minutes,
                        "basis": r.effort_basis,
                        "quote": r.effort_quote,
                        "sessions": r.sessions,
                        "url": r.url,
                        "materials": [
                            {"kind": m.kind, "name": m.name, "detail": m.detail,
                             "quote": m.quote, "basis": m.basis}
                            for m in r.materials
                        ],
                    }
                    for r in rows
                ],
                indent=2,
            )
        )
        raise typer.Exit(0)

    if not rows:
        typer.echo("no assignments recorded — run `backglass coursework --refresh`")
        raise typer.Exit(0)
    for row in rows:
        due = (row.due_at or "")[:10] or "no date"
        sessions = f" ×{row.sessions}" if row.sessions > 1 else ""
        typer.echo(
            f"  {due}  {str(row.effort_minutes or '—'):>4}m{sessions:<4} "
            f"{row.course:<12} {row.title[:52]}  [{row.effort_basis or '—'}]"
        )
        if row.materials:
            typer.echo(
                "          needs: "
                + ", ".join(f"{m.name} ({m.kind})" for m in row.materials)
            )
    typer.echo(f"{len(rows)} assignment(s)")
    raise typer.Exit(0)


@app.command("relevance")
def relevance_command(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the verdicts, write nothing")
    ] = False,
    limit: Annotated[
        int, typer.Option("--limit", help="Obligations judged this run")
    ] = 0,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable")] = False,
) -> None:
    """Retire obligations the owner's own record has overtaken.

    The board gave four blocks of one day to a UT Dallas scholarship deadline while the
    ledger recorded the owner enrolled at ASU. Nothing consumed that fact. This pass does:
    it judges each open obligation against the recorded facts once, drops the ones a fact
    plainly retires — citing that fact in the resolution note — and asks about the rest
    rather than guessing.

    Runs inside `sync`, a bounded slice per pass. `--dry-run` prints the verdicts and
    writes nothing, which is how the first pass on a real ledger should be read.
    """
    import json as _json

    from backglass.extract import prompts
    from backglass.extract import relevance as relevance_mod
    from backglass.telemetry import Metered

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    calls: list[Any] = []
    report = relevance_mod.run(
        conn,
        settings,
        Metered(_build_model_client(settings), "relevance", calls),
        prompt=prompts.load("check-relevance"),
        dry_run=dry_run,
        limit=limit or relevance_mod.PER_RUN,
    )
    if not dry_run:
        conn.commit()

    if as_json:
        typer.echo(_json.dumps({
            "judged": report.judged, "dropped": report.dropped, "asked": report.asked,
            "kept": report.kept, "discarded": report.discarded,
            "cost_usd": round(report.cost_usd, 4), "errors": report.errors,
        }, indent=2))
        raise typer.Exit(1 if report.errors else 0)

    if not report.judged:
        typer.echo("nothing left to judge")
    else:
        # The verdicts first, because a pass costs the same to read as to run: counts
        # alone would make --dry-run pointless.
        for line in report.verdicts:
            typer.echo(f"  {line}")
        typer.echo(
            f"judged {report.judged}: {report.dropped} dropped, {report.asked} asked "
            f"about, {report.kept} kept, {report.discarded} discarded (uncited)"
        )
        typer.echo(f"  spend {round(report.cost_usd * 100)}c")
    if dry_run:
        typer.echo("  nothing written")
    for error in report.errors:
        typer.echo(f"  {error}", err=True)
    raise typer.Exit(1 if report.errors else 0)


@app.command("duplicates")
def duplicates_command(
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Drop the losers in every cluster shown as certain"),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="Clusters to print")] = 12,
) -> None:
    """Review likely-duplicate open commitments, clustered.

    The planner schedules whatever the ledger holds, so one scholarship acceptance
    written down three ways costs two hours of a real day. `dedup.suspects` has always
    found these — 264 open pairs on this ledger — but a pairwise queue that long is one
    nobody finishes, which is why they are still here.

    Prints nothing but a report unless `--apply` is passed, and even then it only touches
    clusters where every member is the same sentence *and* the shape is not a fan-out.
    A fan-out — one message promising an intro email to three instructors — is three real
    promises with identical text, and is always left for the owner.
    """
    from backglass import duplicates as dup_mod
    from backglass.web import actions

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    found = dup_mod.clusters(conn)
    if not found:
        typer.echo("no duplicate suspects")
        return

    certain = [c for c in found if c.identical and c.kind != dup_mod.FAN_OUT]
    typer.echo(
        f"{len(found)} cluster(s) over {sum(len(c.members) for c in found)} open "
        f"commitments — {len(certain)} identical and safe to collapse"
    )
    for cluster in found[:limit]:
        mark = "certain" if cluster in certain else cluster.kind
        typer.echo(f"\n[{mark}]  weakest pair {cluster.weakest:.2f}")
        keep = int(cluster.survivor["id"])
        for member in cluster.members:
            lead = "keep " if int(member["id"]) == keep else "drop "
            who = f"  · {member['who']}" if member["who"] else ""
            typer.echo(f"  {lead}{member['id']:>5}  {str(member['what'])[:66]}{who}")
        if cluster.kind == dup_mod.FAN_OUT:
            # The fact that decides the question, printed rather than acted on.
            typer.echo(
                "        one message, several counterparties — several real promises, "
                "or one org the entity table holds twice (`people merge`)"
            )
    if len(found) > limit:
        typer.echo(f"\n… {len(found) - limit} more; --limit to see them")

    plans, undated = dup_mod.plan_clusters(conn)
    if plans:
        # Engagements have never had a review surface — `dedup.suspects` and everything
        # built on it covers commitments only — and the duplication is worse there.
        # Report-only: closing an engagement automatically is per-type resolution policy,
        # which belongs to the resolver phase and not to a report.
        typer.echo(
            f"\n{len(plans)} day(s) with duplicate plans "
            f"({sum(len(c.members) for c in plans)} engagements)"
        )
        for cluster in plans[:limit]:
            typer.echo(f"\n[plans · {cluster.day}]  weakest pair {cluster.weakest:.2f}")
            keep = int(cluster.survivor["id"])
            for member in cluster.members:
                lead = "keep " if int(member["id"]) == keep else "dup  "
                when = str(member["starts_at"] or "")[11:16] or "--:--"
                typer.echo(
                    f"  {lead}{member['id']:>5}  {when}  {str(member['status'])[:9]:9}"
                    f"  {str(member['what'])[:54]}"
                )
    if undated:
        # Named rather than dropped: most of the twenty-three move-in rows have no date
        # at all, and a report that silently omits them reads as "covered everything".
        typer.echo(
            f"\n{undated} undated engagement(s) not clustered — no day to anchor them in"
        )

    if not apply:
        typer.echo(
            "\nnothing written. `--apply` drops the losers in the certain clusters; "
            "everything else is a question only you can answer:\n"
            "  backglass board  (or /commitments) to merge or keep apart"
        )
        return

    dropped = 0
    for cluster in certain:
        for loser in cluster.losers:
            actions.drop(conn, int(loser["id"]), cluster.note())
            dropped += 1
    conn.commit()
    typer.echo(f"\ndropped {dropped} duplicate(s); each tombstoned with the survivor's id")


@app.command()
def log(
    activity: Annotated[
        str, typer.Argument(help="Activity title or org — a substring is fine")
    ],
    hours: Annotated[int, typer.Argument(help="Hours to add")],
    note: Annotated[
        str | None, typer.Option("--note", help="What you did — raw material for AMCAS")
    ] = None,
    on: Annotated[
        str | None, typer.Option("--on", help="YYYY-MM-DD; defaults to today")
    ] = None,
    new: Annotated[
        str | None,
        typer.Option(
            "--new",
            help="Create the activity first, in this category: "
            + "/".join(activities_mod.CATEGORIES),
        ),
    ] = None,
) -> None:
    """Log hours against an activity. The fast path for the record that matters most.

    The Work & Activities list is the part of a four-year record that cannot be
    rebuilt afterwards — hours are not recoverable from mail, and no one remembers
    them — so the cost of logging is the thing that decides whether the ledger has
    any. One line, from anywhere:

        backglass log "Chen Lab" 3 --note "ran the Western blot"
        backglass log shadowing 4 --on 2026-09-14
        backglass log "Food bank" 2 --new volunteering

    Hours land on the lifetime accumulator the activity's category feeds, as one
    checkpoint — the same write the roadmap page makes, so there is no second source
    of truth and `amcas-export` sees this immediately.
    """

    from backglass.plan import timezones

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)

    if new is not None:
        if new not in activities_mod.CATEGORIES:
            typer.echo(
                f"unknown category {new!r}; expected one of "
                f"{', '.join(activities_mod.CATEGORIES)}",
                err=True,
            )
            raise typer.Exit(code=1)
        # Every reason to refuse has to be found *before* the activity row is written.
        # The connection is autocommit (db/__init__.py), so `add` is durable the moment
        # it runs — a failure after it leaves an activity with no hours, and there is no
        # rename or delete for one anywhere, so the orphan is permanent and consumes an
        # AMCAS slot. The first attempt at this ordered the checks by how obvious they
        # were and left `--on` validation after the write; the resolution below happens
        # in full, for both branches, before anything is created.
        if activities_mod.total_target_for(conn, new) is None:
            typer.echo(
                f"category {new!r} has no lifetime hour target on any active goal, so "
                f"there is nowhere to log {hours}h — nothing was created",
                err=True,
            )
            raise typer.Exit(code=1)

    # Resolve the instant first. `local_now_iso` takes a day only to pick the zone — it
    # always stamps now — so a backdated entry needs `local_noon_iso` on the day named.
    # The owner's "today" is the day in whichever zone they are actually in; `_today`
    # reads `default_tz` only, which refuses a genuine Kolkata today for 12.5 hours of
    # every day while they are there.
    today = timezones.today_for(settings)
    if on:
        day = _day_option(on, "--on")
        assert day is not None  # `on` is truthy, so the option was given
        if day > today:
            # A future entry counts toward the total immediately — the accumulator has no
            # date filter — so it would inflate every progress bar until the day arrived.
            typer.echo(f"--on {on} is in the future; log hours after you do them", err=True)
            raise typer.Exit(code=1)
        if day.year < 2000:
            # Pre-2000 dates fall outside any plausible record and land in local mean
            # time, producing offsets like -07:28:18 that nothing else in the ledger uses.
            typer.echo(f"--on {on} is implausibly old", err=True)
            raise typer.Exit(code=1)
        occurred_at = timezones.local_noon_iso(settings, day)
    else:
        occurred_at = timezones.local_now_iso(settings, today)

    try:
        activities_mod.check_hours(hours)
    except activities_mod.ActivityError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if new is not None:
        try:
            activity_id = activities_mod.add(conn, title=activity, category=new)
        except activities_mod.ActivityError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"new activity: {activity} ({new})")
    else:
        matches = activities_mod.find_by_name(conn, activity)
        if not matches:
            typer.echo(
                f"no activity matching {activity!r} — add it with "
                f"`backglass log {activity!r} {hours} --new <category>`",
                err=True,
            )
            raise typer.Exit(code=1)
        if len(matches) > 1:
            # Filing four years of hours under the wrong activity is not recoverable,
            # so an ambiguous name asks rather than picking the first row.
            typer.echo(f"{activity!r} matches {len(matches)} activities:", err=True)
            for match in matches:
                typer.echo(f"  {match['title']} ({match['category']})", err=True)
            raise typer.Exit(code=1)
        activity_id = int(matches[0]["id"])
    try:
        logged = activities_mod.log_hours(
            conn,
            activity_id=activity_id,
            hours=hours,
            occurred_at=occurred_at,
            note=note,
        )
    except activities_mod.ActivityError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    conn.commit()
    typer.echo(
        f"+{logged.hours}h {logged.activity_title} → {logged.target_title} "
        f"{logged.target_done}/{logged.target_total}"
    )


@app.command("amcas-export")
def amcas_export(
    out: Annotated[
        Path | None, typer.Option("--out", help="Write markdown here instead of stdout")
    ] = None,
) -> None:
    """Assemble the Work & Activities raw material.

    Evidence assembly, never authorship: every hour figure is a sum over named
    checkpoints, the note stream is the owner's own words, and the 700/1325
    character limits are AMCAS's — shown against the draft material, not enforced.
    """
    from backglass.goals import activities

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)

    rows = activities.list_with_hours(conn)
    if not rows:
        typer.echo("no activities recorded — add them on the roadmap page", err=True)
        raise typer.Exit(code=1)

    meaningful = sum(1 for a in rows if a["most_meaningful"])
    lines: list[str] = [
        "# AMCAS Work & Activities — raw material",
        "",
        f"{len(rows)} of {activities.AMCAS_SLOTS} activity slots · "
        f"{meaningful} of {activities.AMCAS_MOST_MEANINGFUL} most-meaningful marked",
        "",
        "Every hour figure below is a sum over the checkpoint ids listed with it.",
        "",
    ]
    for a in rows:
        entries = activities.entries_for(conn, int(a["id"]))
        hour_entries = [e for e in entries if e["kind"] == "total"]
        span = (
            f"{a['first_logged'][:10]} → {a['last_logged'][:10]}"
            if a["first_logged"]
            else "no dates logged"
        )
        lines += [
            f"## {a['title']}"
            + (" · MOST MEANINGFUL" if a["most_meaningful"] else ""),
            "",
            f"- Category: {a['category']}",
            f"- Organization: {a['org'] or '—'}" + (f" · {a['role']}" if a["role"] else ""),
            f"- Contact: {a['contact_name'] or '— (add a supervisor entity)'}",
            f"- Dates: {a['started_on'] or span}"
            + (f" — {a['ended_on']}" if a["ended_on"] else " — ongoing"),
            f"- Total hours: {a['hours']} "
            f"(checkpoints {', '.join(str(e['id']) for e in hour_entries) or 'none'})",
            "",
        ]
        notes = [e for e in entries if e["note"]]
        material = " ".join(str(e["note"]) for e in notes)
        limit = (
            activities.AMCAS_MEANINGFUL_CHARS
            if a["most_meaningful"]
            else activities.AMCAS_DESCRIPTION_CHARS
        )
        lines.append(
            f"Draft material: {len(material)} chars against the {limit}-char limit"
            + (" (700 description + 1325 remarks)" if a["most_meaningful"] else "")
        )
        lines += [f"- {e['occurred_at'][:10]} · +{e['delta']} · {e['note']}" for e in notes]
        lines.append("")

    text = "\n".join(lines)
    if out is not None:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(text)


@roadmap_app.command("paths")
def roadmap_paths() -> None:
    """List the preset paths in specs/roadmaps/."""
    from backglass.roadmap import presets

    for p in presets.list_paths():
        typer.echo(f"{p.id}  v{p.version}  {p.title}  ({len(p.steps)} steps)")


@roadmap_app.command("start")
def roadmap_start(
    path: str,
    start: Annotated[str | None, typer.Option("--start", help="YYYY-MM-DD")] = None,
    no_interview: Annotated[
        bool, typer.Option("--no-interview", help="Use the preset as written")
    ] = False,
) -> None:
    """Instantiate a path — after a short personalization interview, unless told not to."""

    from backglass.roadmap import instantiate, interview, presets

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    try:
        preset = presets.load(path)
    except presets.PresetError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    start_date = _day_option(start, "--start") or _today(settings)

    result = None
    adjustments = None
    if not no_interview:
        client = _build_model_client(settings)
        result = interview.run_interview(
            conn, settings, client, preset, start_date,
            ask=lambda q: typer.prompt(q, default="", show_default=False),
            say=lambda s: typer.echo(f"\n{s}\n"),
        )
        if result.degraded_reason:
            typer.echo(result.degraded_reason)
        adjustments = result.adjustments

    roadmap_id = instantiate.instantiate(conn, settings, preset, start_date, adjustments)
    if result is not None:
        interview.persist(conn, roadmap_id, result)
    label = "personalized" if adjustments else "as written"
    typer.echo(f"roadmap {roadmap_id} started ({label}): {preset.title}")


@roadmap_app.command("list")
def roadmap_list() -> None:
    from backglass.web.routes.roadmaps import list_roadmaps

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    rows = list_roadmaps(conn)
    if not rows:
        typer.echo("no roadmaps. `backglass roadmap paths` shows what can be started.")
        raise typer.Exit()
    for r in rows:
        typer.echo(
            f"[{r['id']}] {r['title']}  {r['done_steps']}/{r['live_steps']} steps"
            f"  {r['status']}"
            + (f"  target {str(r['target_date'])[:10]}" if r["target_date"] else "")
        )


@roadmap_app.command("show")
def roadmap_show(roadmap_id: int) -> None:
    from backglass.web.routes.roadmaps import roadmap_detail

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    detail = roadmap_detail(conn, roadmap_id)
    if detail is None:
        typer.echo(f"no roadmap {roadmap_id}", err=True)
        raise typer.Exit(code=1)
    r = detail["r"]
    typer.echo(f"{r['title']}  ({r['path_id']} v{r['path_version']}, {r['status']})")
    typer.echo(f"done means: {r['definition_of_done']}")
    marks = {"done": "x", "skipped": "-", "pending": " ", "active": ">"}
    for s in detail["steps"]:
        planned = str(s["planned_date"] or "")[:10]
        mark = marks.get(str(s["status"]), " ")
        typer.echo(f"  [{mark}] {planned}  {s['title']}  (step {s['id']})")
    for c in detail["cadences"]:
        typer.echo(f"  ~ {c['title']}: {c['weekly_count']}/wk")


@roadmap_app.command("step")
def roadmap_step(
    step_id: int,
    done: Annotated[bool, typer.Option("--done")] = False,
    skip: Annotated[bool, typer.Option("--skip")] = False,
    unskip: Annotated[bool, typer.Option("--unskip")] = False,
    on_date: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD")] = None,
    up: Annotated[bool, typer.Option("--up")] = False,
    down: Annotated[bool, typer.Option("--down")] = False,
) -> None:
    """Edit one step: --done | --skip | --unskip | --date | --up | --down."""

    from backglass.roadmap import adjust

    chosen = [x for x, on in
              [("done", done), ("skip", skip), ("unskip", unskip),
               ("date", on_date is not None), ("up", up), ("down", down)] if on]
    if len(chosen) != 1:
        typer.echo("pass exactly one of --done/--skip/--unskip/--date/--up/--down", err=True)
        raise typer.Exit(code=1)
    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    try:
        if done:
            adjust.complete_step(conn, step_id)
        elif skip:
            adjust.skip_step(conn, step_id)
        elif unskip:
            adjust.unskip_step(conn, step_id)
        elif on_date is not None:
            redated = _day_option(on_date, "--date")
            assert redated is not None  # `on_date is not None`, so the option was given
            adjust.redate_step(conn, step_id, redated)
        else:
            adjust.move_step(conn, step_id, "up" if up else "down")
    except adjust.AdjustError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"step {step_id}: {chosen[0]}")


# ── Phase 7: source toggle ────────────────────────────────────────────────

sources_app = typer.Typer(
    invoke_without_command=True, help="List, pause, and resume sources."
)
app.add_typer(sources_app, name="sources")


@sources_app.callback()
def sources_list(ctx: typer.Context) -> None:
    """Every credential row with its enabled state; config-only sources show once
    they have synced (or been toggled) at least once."""
    if ctx.invoked_subcommand is not None:
        return
    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    rows = list(
        conn.execute(
            "SELECT source, status, enabled, updated_at FROM credential"
            " WHERE user_id = ? ORDER BY enabled DESC, source",
            (USER_ID,),
        )
    )
    if not rows:
        typer.echo("no sources have synced yet")
        raise typer.Exit()
    for r in rows:
        state = "paused" if not r["enabled"] else str(r["status"])
        typer.echo(f"{'·' if r['enabled'] else '×'} {r['source']:<18} {state}")


@sources_app.command("disable")
def sources_disable(source: str) -> None:
    """Pause a source. Keeps its credential, cursor, and every stored item."""
    from backglass.connectors import credentials as cred_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    cred_mod.set_enabled(conn, source, False)
    typer.echo(f"{source} paused — sync will skip it until `sources enable {source}`")


@sources_app.command("enable")
def sources_enable(source: str) -> None:
    from backglass.connectors import credentials as cred_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    cred_mod.set_enabled(conn, source, True)
    typer.echo(f"{source} resumed")


# ── learned noise ────────────────────────────────────────────────────────

noise_app = typer.Typer(
    invoke_without_command=True,
    help="Senders the triage model keeps dropping, promoted to free rule drops.",
)
app.add_typer(noise_app, name="noise")


@noise_app.callback()
def noise_list(ctx: typer.Context) -> None:
    """Enabled learned rules, most recently promoted first."""
    if ctx.invoked_subcommand is not None:
        return
    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    rows = list(
        conn.execute(
            "SELECT kind, value, evidence_count, promoted_at, promoted_by, enabled"
            " FROM learned_noise WHERE user_id = ? ORDER BY promoted_at DESC",
            (USER_ID,),
        )
    )
    if not rows:
        typer.echo("nothing learned yet — `backglass noise suggest` mines the history")
        raise typer.Exit()
    for r in rows:
        mark = "·" if r["enabled"] else "×"
        typer.echo(
            f"{mark} {r['kind']:<7} {r['value']:<36} {r['evidence_count']:>3} drops"
            f"  {r['promoted_at'][:10]} ({r['promoted_by']})"
        )


@noise_app.command("suggest")
def noise_suggest(
    min_evidence: Annotated[
        int, typer.Option("--min-evidence", help="Observations required (drops + barren keeps)")
    ] = 5,
) -> None:
    """Dry-run: who would be promoted, with the evidence. Writes nothing."""
    from backglass.extract import noise as noise_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    found = noise_mod.candidates(conn, settings, min_evidence=min_evidence)
    if not found:
        typer.echo("no candidates — every repeat-dropped sender is already covered")
        raise typer.Exit()
    typer.echo(
        "candidates (nothing ever came of their mail; a sender's first real ask after"
        " promotion would be lost — promote deliberately):"
    )
    for c in found:
        window = f"{(c.first_seen or '')[:10]}..{(c.last_seen or '')[:10]}"
        # The two evidence classes are printed apart, not summed. "78 barren" means the
        # expensive pass read 78 of this sender's mails and found nothing in any of
        # them; "78 drops" means the triage model never let them through. The owner is
        # deciding whether to stop reading a sender forever and should see which.
        counts = f"{c.model_drops:>3} drops {c.barren_bulk_keeps:>3} barren"
        typer.echo(f"  {c.kind:<7} {c.value:<36} {counts}  {window}")
        # A sample of what they actually write. A domain reads as junk in a list right
        # up until the one letter a year that carries a real deadline.
        detail = c.sample_title or c.sample_reason
        if detail:
            typer.echo(f"          e.g. {detail[:76]}")
    typer.echo("promote with: backglass noise promote <value> | --all")


@noise_app.command("promote")
def noise_promote(
    value: Annotated[str | None, typer.Argument(help="Address or domain")] = None,
    all_: Annotated[
        bool, typer.Option("--all", help="Promote every address candidate")
    ] = False,
    min_evidence: Annotated[int, typer.Option("--min-evidence")] = 5,
) -> None:
    """Promote candidates into free tier-0 drops. Refuses any sender that ever produced."""
    from backglass.extract import noise as noise_mod

    if not value and not all_:
        typer.echo("give an address/domain, or --all for every address candidate", err=True)
        raise typer.Exit(2)
    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    found = noise_mod.candidates(conn, settings, min_evidence=min_evidence)
    if all_:
        chosen = [c for c in found if c.kind == "address"]
    else:
        chosen = [c for c in found if c.value == (value or "").strip().lower()]
        if not chosen:
            # Not a candidate: either unknown, insufficient evidence, or it has a keep
            # somewhere in history. No --force by design — the env NOISE_SENDERS line
            # remains the owner's unconditional override channel.
            typer.echo(
                f"{value!r} is not promotable: no candidate with {min_evidence}+ model"
                " drops and zero keeps. NOISE_SENDERS in .env is the manual override.",
                err=True,
            )
            raise typer.Exit(1)
    written = noise_mod.promote(conn, chosen, by="cli")
    conn.commit()
    typer.echo(f"promoted {written} rule(s); future syncs drop them for free")


@noise_app.command("templates")
def noise_templates(
    backfill: Annotated[
        bool, typer.Option("--backfill", help="Hash rows that predate migration 0010")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="Templates to show")] = 15,
) -> None:
    """Recurring mail shapes: counts, verdict mix, sample titles. Audit surface for
    every `template:<hash8>` drop reason."""
    from backglass.extract import templates as templates_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)

    if backfill:
        rows = list(
            conn.execute(
                "SELECT id, author, title, body_text FROM source_item"
                " WHERE user_id = ? AND template_hash IS NULL",
                (USER_ID,),
            )
        )
        filled = 0
        for r in rows:
            th = templates_mod.template_hash(
                author=r["author"], title=r["title"], body_text=r["body_text"]
            )
            if th is not None:
                conn.execute(
                    "UPDATE source_item SET template_hash = ? WHERE id = ?", (th, r["id"])
                )
                filled += 1
        conn.commit()
        typer.echo(f"backfilled {filled} of {len(rows)} unhashed row(s)")

    top = list(
        conn.execute(
            "SELECT template_hash, COUNT(*) AS n,"
            " SUM(CASE WHEN triage_verdict = 'drop' THEN 1 ELSE 0 END) AS dropped,"
            " SUM(CASE WHEN triage_verdict = 'keep' THEN 1 ELSE 0 END) AS kept,"
            " MAX(title) AS sample_title, MAX(author) AS sample_author"
            " FROM source_item"
            " WHERE user_id = ? AND template_hash IS NOT NULL"
            " GROUP BY template_hash HAVING n > 1"
            " ORDER BY n DESC LIMIT ?",
            (USER_ID, limit),
        )
    )
    if not top:
        typer.echo("no repeated templates yet")
        raise typer.Exit()
    for t in top:
        typer.echo(
            f"{t['template_hash'][:8]}  ×{t['n']:<3} drop {t['dropped']}/keep {t['kept']}"
            f"  {t['sample_author']}: {(t['sample_title'] or '')[:48]}"
        )


@noise_app.command("remove")
def noise_remove(
    value: Annotated[str, typer.Argument(help="Previously promoted value")],
    requeue: Annotated[
        bool, typer.Option("--requeue", help="Send its dropped items back to triage")
    ] = False,
) -> None:
    """Disable a learned rule (row kept for audit). --requeue re-triages its drops."""
    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    value = value.strip().lower()
    cursor = conn.execute(
        "UPDATE learned_noise SET enabled = 0 WHERE user_id = ? AND value = ?",
        (USER_ID, value),
    )
    if cursor.rowcount == 0:
        typer.echo(f"no learned rule for {value!r}", err=True)
        raise typer.Exit(1)
    requeued = 0
    if requeue:
        # Exactly the reasons rules.classify writes for a noise hit; migration 0002
        # explicitly permits clearing triage columns back to NULL.
        requeued = conn.execute(
            "UPDATE source_item SET triage_verdict = NULL, triage_reason = NULL"
            " WHERE user_id = ? AND triage_reason IN (?, ?)",
            (
                USER_ID,
                f"known-noise sender: {value}",
                f"known-noise domain: {value}",
            ),
        ).rowcount
    conn.commit()
    typer.echo(
        f"disabled {value}"
        + (f"; {requeued} item(s) back in the triage queue" if requeue else "")
    )


# ── costs ────────────────────────────────────────────────────────────────

costs_app = typer.Typer(
    invoke_without_command=True,
    help="Where the model spend goes. Approximate below the run level.",
)
app.add_typer(costs_app, name="costs")


@costs_app.callback()
def costs_summary(ctx: typer.Context) -> None:
    """Month-to-date spend vs cap, projection, and where the extraction money goes."""
    if ctx.invoked_subcommand is not None:
        return
    from backglass import costs as costs_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)

    m = costs_mod.month(conn, settings)
    if m.spend_is_imputed:
        # Nothing is billed on a subscription backend, so there is no percentage of a cap
        # to report and no overrun to warn about. The figure is still worth printing —
        # it is what the month would have cost on the API — but it is labelled as that.
        typer.echo(
            f"month-to-date  {m.spend_cents}c imputed (subscription backend"
            f" {settings.model_backend}; nothing billed, cap not enforced)"
            f" · projected {m.projected_cents}c by month end"
        )
    else:
        pct = round(100 * m.spend_cents / m.cap_cents) if m.cap_cents else 0
        over = "  ← projected to exceed the cap" if m.projected_cents > m.cap_cents else ""
        typer.echo(
            f"month-to-date  {m.spend_cents}c of {m.cap_cents}c cap ({pct}%)"
            f" · projected {m.projected_cents}c by month end{over}"
        )
    avg = f"{m.avg_cents_per_item:.1f}c" if m.extracted else "—"
    degraded = f" ({m.degraded_runs} degraded)" if m.degraded_runs else ""
    typer.echo(
        f"runs           {m.runs}{degraded} · extraction {m.extracted} items"
        f" · ≈{avg} per extracted item"
    )
    # Told by cause, not by the boolean. A run the usage window stopped never reached the
    # cap — its items are pending rather than parked and clear by themselves — so folding
    # it into the cap's line names a wall that was not hit and a month-end release that
    # does not apply.
    if m.rate_limited_runs:
        typer.echo(
            f"  ← {m.rate_limited_runs} run(s) hit a model rate limit this month; what "
            "they left is still pending, and later syncs retry it",
            err=True,
        )
    if m.capped_runs and not m.spend_is_imputed:
        typer.echo(
            "  ← spend cap was reached this month; extraction has been skipped", err=True
        )
    elif m.capped_runs:
        typer.echo(
            f"  ← {m.capped_runs} run(s) degraded before the backend was known to be "
            "unbilled; re-run `backglass sync` to extract what they left pending",
            err=True,
        )

    trend = costs_mod.kill_trend(conn)
    if trend:
        rates = " → ".join(f"{round(100 * (r['kill_rate'] or 0))}%" for r in reversed(trend))
        typer.echo(
            f"triage kill    {rates} (last {len(trend)} weeks; <85% means rules drifted)"
        )

    senders = costs_mod.top_senders(conn, m)
    if senders:
        typer.echo(
            "\ntop senders by extraction volume (≈ items × avg — spend is recorded"
            " per run,\nnot per item, so per-sender cost is an estimate):"
        )
        for s in senders:
            typer.echo(
                f"  {(s['author'] or '<unknown>'):<32} {s['items_extracted']:>4} items"
                f"  ≈{s['approx_cents']}c  {s['commitments']} commitments"
            )


@costs_app.command("calls")
def costs_calls(
    days: Annotated[int, typer.Option("--days", help="Window")] = 7,
) -> None:
    """What a model call costs and takes, per tier. Phase 0 of the backend plan.

    `run.spend_cents` is one total over two tiers and answers none of the questions the
    plan turns on. This does: median and p95 wall-clock, mean payload, and cost per
    call, so the case for batching extraction is measured rather than argued.
    """
    from backglass import costs as costs_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    rows = costs_mod.by_call_tier(conn, days=days)
    if not rows:
        typer.echo(
            f"no calls recorded in {days}d — model_call starts filling on the next sync"
        )
        raise typer.Exit()
    typer.echo(f"{'tier':<14}{'calls':>7}{'fail':>6}{'median':>9}{'p95':>9}"
               f"{'chars':>9}{'cents':>9}{'per call':>10}")
    for r in rows:
        typer.echo(
            f"{r['tier']:<14}{r['calls']:>7}{r['failures']:>6}"
            f"{str(r['median_ms']) + 'ms':>9}{str(r['p95_ms']) + 'ms':>9}"
            f"{int(r['mean_chars'] or 0):>9}{r['cents']:>9}{r['cents_per_call']:>10}"
        )
    typer.echo("cost is imputed on subscription auth — see `backglass costs`")


@costs_app.command("runs")
def costs_runs(
    limit: Annotated[int, typer.Option("--limit", help="How many runs")] = 15,
) -> None:
    """Spend and volume by run, newest first."""
    from backglass import costs as costs_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    rows = costs_mod.by_run(conn, limit=limit)
    if not rows:
        typer.echo("no runs recorded yet")
        raise typer.Exit()
    for r in rows:
        # NULL degrade_reason on a degraded row predates 0016, when the cap was the only
        # thing that could pause a run.
        degraded = f"degraded:{r['degrade_reason'] or 'spend_cap'}" if r["degraded"] else ""
        flags = degraded + (" errors" if r["had_errors"] else "")
        typer.echo(
            f"{r['started_at'][:16]}  fetched {r['items_fetched']:>4}"
            f"  out {r['items_triaged_out']:>4}  extracted {r['items_extracted']:>3}"
            f"  writes {r['writes']:>3}  {r['spend_cents']:>4}c  {flags.strip()}"
        )


@costs_app.command("senders")
def costs_senders(
    limit: Annotated[int, typer.Option("--limit", help="How many senders")] = 25,
) -> None:
    """Full top-senders table. Estimated cost — see the caveat in the summary."""
    from backglass import costs as costs_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    m = costs_mod.month(conn, settings)
    senders = costs_mod.top_senders(conn, m, limit=limit)
    if not senders:
        typer.echo("nothing extracted this month")
        raise typer.Exit()
    for s in senders:
        typer.echo(
            f"{(s['author'] or '<unknown>'):<40} {s['items_extracted']:>4} items"
            f"  ≈{s['approx_cents']}c  {s['commitments']} commitments"
        )


# ── audit: reading the owner's own corrections back ──────────────────────

# Its own family rather than a `noise` subcommand, because the two look at different
# tiers and would answer different questions under one name: `noise` mines tier-0/1
# triage to stop paying for mail, while this reads tier-2 extraction quality out of the
# owner's review-queue verdicts. Filing "why are the extractions wrong" under "which
# senders are junk" would bury it. `audit` is left deliberately open for the other
# read-back surfaces that belong beside it.
audit_app = typer.Typer(help="Read the owner's own corrections back into the build loop.")
app.add_typer(audit_app, name="audit")


@audit_app.command("corrections")
def audit_corrections(
    days: Annotated[int, typer.Option("--days", help="Trailing window")] = 30,
) -> None:
    """What the review queue has been teaching, and which file to go and edit.

    Every accept and reject on the dashboard is labelled training signal; this is the
    only thing that reads it. Below ~50 corrections it says so instead of drawing a
    distribution over noise.
    """
    from backglass import corrections as corrections_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    rep = corrections_mod.report(conn, days=days)

    if not rep.total:
        typer.echo(
            f"no owner corrections in the last {days} days. Accept/reject in the"
            " dashboard's review queue is what fills this — nothing else writes it."
        )
        raise typer.Exit()

    typer.echo(
        f"{rep.total} corrections in {days}d  ·  {rep.accepts} accepted,"
        f" {rep.rejects} rejected"
    )
    if not rep.meaningful:
        # The whole reason this command exists is to be acted on, so a sample that
        # cannot support an action prints its size and stops. Everything below here
        # would be a shape read out of fewer rows than it has categories.
        typer.echo(
            f"{rep.total} corrections so far; the distribution is not meaningful below"
            f" ~{corrections_mod.MEANINGFUL_MINIMUM}. Come back with more."
        )
        raise typer.Exit()

    typer.echo("\nwhy the owner rejected:")
    for reason, n in sorted(rep.reasons.items(), key=lambda kv: (-kv[1], kv[0])):
        share = round(100 * n / rep.rejects) if rep.rejects else 0
        typer.echo(f"  {corrections_mod.reason_label(reason):<18} {n:>4}  {share:>3}%")

    by_source = rep.reasons_by_source
    if len(by_source) > 1:
        typer.echo("\n  by source:")
        for source, reasons in sorted(by_source.items()):
            mix = ", ".join(
                f"{corrections_mod.reason_label(r)} {n}"
                for r, n in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
            )
            typer.echo(f"    {source:<12} {mix}")

    by_version = rep.reasons_by_version
    if len(by_version) > 1:
        # One version in the window means no prompt changed in it, and a single-row
        # "split" implies a comparison that was never made.
        typer.echo("\n  by extraction prompt version:")
        for version, reasons in sorted(by_version.items()):
            mix = ", ".join(
                f"{corrections_mod.reason_label(r)} {n}"
                for r, n in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
            )
            typer.echo(f"    {version:<12} {mix}")

    typer.echo(
        f"\nconfidence calibration (the model's own score vs the owner's verdict;"
        f"\nthreshold is {settings.confidence_threshold:.2f} — a band well below its"
        " accept rate is a threshold set too high):"
    )
    for b in rep.buckets:
        typer.echo(
            f"  {b.low:.1f}–{b.low + 0.1:.1f}  {b.total:>4} judged"
            f"  {round(100 * b.accept_rate):>3}% accepted"
        )

    typer.echo("\nrejection rate by source, then by sender (of what reached the queue):")
    for s in rep.worst_sources:
        typer.echo(
            f"  {s.key:<32} {s.rejected:>3}/{s.total:<3} {round(100 * s.reject_rate):>3}%"
        )
    for s in rep.worst_senders[:5]:
        typer.echo(
            f"    {s.key[:40]:<40} {s.rejected:>3}/{s.total:<3}"
            f" {round(100 * s.reject_rate):>3}%"
        )

    remedy = rep.remedy
    if remedy is None:
        typer.echo("\nno single dominant reject reason — no one file to point at yet.")
    else:
        typer.echo(
            f"\n→ {corrections_mod.reason_label(rep.dominant_reason or '')} dominates"
            f" ({rep.reasons[rep.dominant_reason or '']} of {rep.rejects}): edit {remedy}"
        )


# ── batch mode ───────────────────────────────────────────────────────────

batch_app = typer.Typer(
    help="Overnight extraction through the Message Batches API, at half price."
)
app.add_typer(batch_app, name="batch")


def _require_anthropic_key(settings: Settings) -> None:
    from backglass.extract.client import anthropic_api_key

    if not anthropic_api_key(settings):
        typer.echo(
            "batch mode needs the Anthropic API — set MODEL_API_KEY or "
            "ANTHROPIC_API_KEY (pairs best with MODEL_BACKEND=anthropic)",
            err=True,
        )
        raise typer.Exit(2)


@batch_app.command("submit")
def batch_submit() -> None:
    """Ingest + rules + triage now; package pending extraction into a batch."""
    from backglass import batch as batch_mod

    settings = get_settings()
    _require_anthropic_key(settings)
    conn = _open(settings)
    migrate(conn)
    connectors = _all_connectors(conn, settings)
    try:
        report = batch_mod.submit(
            conn, settings, connectors, model_client.build(settings)
        )
    except SyncLocked as locked:
        typer.echo(f"{locked}; skipped")
        raise typer.Exit(0) from None
    typer.echo(
        f"fetched {report.fetched} · triaged {report.triaged} · "
        f"batched {report.batched}"
        + (f" as {report.batch_id}" if report.batch_id else " (nothing to batch)")
    )
    if report.skipped_oversize or report.skipped_for_cap:
        typer.echo(
            f"skipped: {report.skipped_oversize} oversize, "
            f"{report.skipped_for_cap} beyond the spend cap"
        )
    for error in report.errors:
        typer.echo(f"  {error}", err=True)
    raise typer.Exit(report.exit_code)


@batch_app.command("collect")
def batch_collect() -> None:
    """Apply finished batch results through the same ledger paths sync uses."""
    from backglass import batch as batch_mod

    settings = get_settings()
    _require_anthropic_key(settings)
    conn = _open(settings)
    migrate(conn)
    try:
        report = batch_mod.collect(conn, settings)
    except SyncLocked as locked:
        typer.echo(f"{locked}; skipped")
        raise typer.Exit(0) from None
    if not report.batches and not report.still_processing and not report.errors:
        typer.echo("no outstanding batches")
        raise typer.Exit()
    typer.echo(
        f"collected {report.batches} batch(es) · extracted {report.extracted}"
        f" ({report.already_done} already done, {report.failed_items} failed)"
        f" · spend {report.spend_cents}c (batch −50%)"
    )
    if report.still_processing:
        typer.echo(f"{report.still_processing} batch(es) still processing — retry later")
    # The recognition loop ran inside `collect`, under its lock. Printed here because the
    # report carries it: the chain no longer belongs to whichever command remembered it.
    for line in report.loop_lines:
        typer.echo(line)
    for error in report.errors:
        typer.echo(f"  {error}", err=True)
    for failure in report.loop_failed:
        typer.echo(f"  {failure}", err=True)
    raise typer.Exit(report.exit_code)


@batch_app.command("status")
def batch_status() -> None:
    """Every batch, newest first."""
    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    rows = list(
        conn.execute(
            "SELECT mb.batch_id, mb.status, mb.created_at, mb.spend_cents,"
            " COUNT(mbi.id) AS items"
            " FROM model_batch mb LEFT JOIN model_batch_item mbi"
            "   ON mbi.batch_id = mb.batch_id"
            " WHERE mb.user_id = ? GROUP BY mb.batch_id ORDER BY mb.id DESC LIMIT 20",
            (USER_ID,),
        )
    )
    if not rows:
        typer.echo("no batches submitted yet")
        raise typer.Exit()
    for r in rows:
        typer.echo(
            f"{r['created_at'][:16]}  {r['batch_id']:<28} {r['status']:<10}"
            f" {r['items']:>3} items  {r['spend_cents']:>4}c"
        )


# ── Keeping people warm ───────────────────────────────────────────────────


reachout_app = typer.Typer(help="Log a touch, and set how often one is due.")
app.add_typer(reachout_app, name="touch")


@reachout_app.command("log")
def touch_log(
    person: Annotated[str, typer.Argument(help="Name or entity id")],
    kind: Annotated[str, typer.Option("--kind", "-k", help="met | sent | call | note")] = "met",
    on: Annotated[
        str | None, typer.Option("--on", help="YYYY-MM-DD; defaults to today")
    ] = None,
    note: Annotated[str | None, typer.Option("--note", "-n", help="What happened")] = None,
) -> None:
    """Record a touch the ledger cannot see — a dinner, a call, a message you sent.

    This is what makes the reminder work for someone you only ever meet in person: the
    touch becomes a manual source item, so the brief has provenance to cite and the
    clock restarts from a real date rather than from silence.
    """
    from backglass.people import reachout as reachout_mod
    from backglass.people import touch as touch_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    try:
        entity_id = reachout_mod.resolve(conn, person)
        touch_id = touch_mod.record(
            conn, settings, entity_id, kind=kind, occurred_at=on, note=note
        )
    except (reachout_mod.ReachoutError, touch_mod.TouchError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    conn.commit()

    warmth = next(
        (t for t in touch_mod.cold(conn, settings, _today(settings))
         if t.entity_id == entity_id),
        None,
    )
    where = f" — {warmth.chip()}" if warmth else ""
    typer.echo(f"touchpoint {touch_id}: {kind} with #{entity_id}{where}")


@reachout_app.command("every")
def touch_every(
    person: Annotated[str, typer.Argument(help="Name or entity id")],
    days: Annotated[
        int | None,
        typer.Argument(help="Days between touches; omit to fall back to the defaults"),
    ] = None,
) -> None:
    """Set how often this person is worth a touch. Due at the cadence, overdue at twice."""
    from backglass.people import reachout as reachout_mod
    from backglass.people import touch as touch_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    try:
        entity_id = reachout_mod.resolve(conn, person)
        touch_mod.set_cadence(conn, entity_id, days)
    except (reachout_mod.ReachoutError, touch_mod.TouchError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    conn.commit()
    if days:
        typer.echo(f"#{entity_id}: due every {days} days, overdue at {days * 2}")
    else:
        typer.echo(
            f"#{entity_id}: back to the defaults — due at "
            f"{settings.people_touch_warn_days}, overdue at {settings.people_touch_cold_days}"
        )


@app.command()
def reachout(
    person: Annotated[
        str | None,
        typer.Argument(help="Name or entity id; omit to list who is going quiet"),
    ] = None,
    template: Annotated[
        str, typer.Option("--template", "-t", help="thanks | warm | ask")
    ] = "thanks",
    note: Annotated[
        str | None,
        typer.Option("--note", "-n", help="The specific thing you want to say, in your words"),
    ] = None,
    where: Annotated[
        str | None,
        typer.Option("--where", help="Where you met — 'at the welcome dinner'"),
    ] = None,
    subject: Annotated[
        str | None, typer.Option("--subject", help="Override the template's subject line")
    ] = None,
    due: Annotated[
        bool, typer.Option("--due", help="Only those at or past their cadence")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable")] = False,
) -> None:
    """Draft an email that keeps a connection warm.

    Deterministic — no model call, so this costs nothing and reads the same way twice.
    The `--note` is the whole draft: the specific thing that passed between you two,
    in your words. Everything else is scaffolding around it.

    Nothing is written. The draft becomes evidence when you send it and the reply
    arrives through the mail connector like any other item.
    """
    from backglass.people import reachout as reachout_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    day = _today(settings)

    if person is None:
        rows = reachout_mod.candidates(conn, settings, day)
        if due:
            rows = [row for row in rows if row.level in ("warn", "cold")]
        if not rows:
            typer.echo(
                "nobody is due" if due else
                "nobody is going quiet — only curated profiles (a role, an org or a tag)"
                " are tracked, so a bare name will not appear here"
            )
            return
        typer.echo(f"{len(rows)} {'due' if due else 'to consider'}, coldest first:")
        for row in rows:
            label = " · ".join(
                part for part in (row.role, row.org, row.cadence_chip()) if part
            )
            mark = {"cold": "!!", "warn": "!", "new": " ·"}.get(row.level, "  ")
            typer.echo(
                f"  {mark} #{row.entity_id:<5} {row.name:<28} {row.chip()}"
                f"{('  (' + label + ')') if label else ''}"
            )
        typer.echo(
            '\ndraft one:  backglass reachout "<name>" --template thanks'
            ' --note "<the specific thing>"'
            '\nlog one:    backglass touch log "<name>" --kind met --note "<what happened>"'
        )
        return

    try:
        entity_id = reachout_mod.resolve(conn, person)
        record = reachout_mod.draft(
            conn,
            settings,
            entity_id,
            note=note or "",
            template=template,
            where=where,
            subject=subject,
            day=day,
        )
    except reachout_mod.ReachoutError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    if as_json:
        typer.echo(reachout_mod.to_json(record))
        return

    typer.echo(record.as_text())
    typer.echo("\n── where each part came from ──")
    for line in record.evidence:
        typer.echo(f"  · {line}")


# ── Phase A2: setup ───────────────────────────────────────────────────────


@app.command()
def setup(
    yes: Annotated[
        bool, typer.Option("--yes", help="Enable everything found without asking")
    ] = False,
    env_path: Annotated[
        Path, typer.Option("--env-path", help="Env file to write")
    ] = Path(".env"),
    reviews_target: Annotated[
        int | None,
        typer.Option("--reviews-target", help="Cadence target id for review checkpoints"),
    ] = None,
) -> None:
    """Hook up every source this machine can offer, in one sitting.

    Detection finds the local stores (Anki, Avorio, Messages, Obsidian, the Apple
    bridges) at their well-known locations and writes the .env lines the owner
    would otherwise hunt for; the token/OAuth sources get their exact remaining
    step printed rather than pretended away. Read-only until confirmed, idempotent
    once configured — running it again reports "configured" and writes nothing.
    """
    from backglass import envfile
    from backglass.connectors import detect as detect_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    authed = {
        str(row["source"])
        for row in conn.execute(
            "SELECT source FROM credential WHERE user_id = ? AND status = 'ok'", (USER_ID,)
        )
    }
    detections = detect_mod.detect_all(settings, authed=authed)

    marks = {
        detect_mod.CONFIGURED: "ok ",
        detect_mod.FOUND: "new",
        detect_mod.NEEDS_SETUP: "…  ",
        detect_mod.MISSING: "-- ",
    }
    for d in detections:
        typer.echo(f"[{marks[d.status]}] {d.source:<18} {d.hint}")

    updates: dict[str, str] = {}
    for d in detections:
        if d.status != detect_mod.FOUND or d.env_key is None or d.env_value is None:
            continue
        if yes or typer.confirm(f"enable {d.source}?", default=True):
            updates[d.env_key] = d.env_value

    # ── bind the reviews target while we are here ──────────────────────────
    wants_reviews = (
        settings.reviews_target_id is None
        and ("ANKI_DB_PATH" in updates or "AVORIO_DB_PATH" in updates
             or settings.anki_db_path or settings.avorio_db_path)
    )
    if wants_reviews:
        cadences = list(conn.execute(
            "SELECT t.id, t.title, g.title AS goal FROM target t "
            "JOIN goal g ON g.id = t.goal_id "
            "WHERE t.kind = 'cadence' AND t.active = 1 AND g.status = 'active' "
            "ORDER BY t.id"
        ))
        chosen: int | None = None
        if reviews_target is not None:
            chosen = reviews_target
        elif cadences and not yes:
            typer.echo("review-day checkpoints can advance a cadence target:")
            for c in cadences:
                typer.echo(f"  {c['id']:>4}  {c['goal']} — {c['title']}")
            picked = typer.prompt("target id (0 to skip)", type=int, default=0)
            chosen = picked or None
        elif cadences:
            typer.echo(
                "REVIEWS_TARGET_ID not set — rerun with --reviews-target <id> to bind "
                "review streaks to a goal (candidates: "
                + ", ".join(f"{c['id']}={c['title']}" for c in cadences) + ")"
            )
        if chosen is not None:
            if not any(int(c["id"]) == chosen for c in cadences):
                typer.echo(f"no active cadence target {chosen}", err=True)
                raise typer.Exit(code=1)
            updates["REVIEWS_TARGET_ID"] = str(chosen)

    if updates:
        # The template beside the target file wins; the repo-root copy is the
        # fallback so a run from outside the checkout still seeds the documented
        # file instead of a two-line orphan.
        template = env_path.with_name(".env.example")
        if not template.exists():
            template = Path(__file__).resolve().parent.parent / ".env.example"
        try:
            changed = envfile.set_keys(env_path, updates, template=template)
        except envfile.EnvValueError as exc:
            # Detection found a path the .env cannot represent on one line. Stop with the
            # offending value shown rather than write a file whose extra assignment would
            # silently override a real setting on the next load.
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        for key in changed:
            typer.echo(f"wrote {key} to {env_path}")
        if changed:
            typer.echo("restart the dashboard/sync for the new sources to load")
    else:
        typer.echo("nothing to write")

    remaining = [d for d in detections if d.status == detect_mod.NEEDS_SETUP]
    if remaining:
        typer.echo("still needs a step only you can do:")
        for d in remaining:
            typer.echo(f"  {d.source}: {d.hint}")
    typer.echo("finish with `backglass doctor`")


# ── backup and restore (audit #23) ────────────────────────────────────────


@app.command()
def backup() -> None:
    """Snapshot the ledger, then delete snapshots outside the keep window.

    The database is the only copy of the record and docs/08 keeps `data/` out of every
    cloud sync, so this is the safety net. Runs daily at 02:00 via
    `launchd/templates/com.backglass.backup.plist.tmpl`.
    """
    from backglass import backup as backup_mod

    settings = get_settings()
    try:
        path = backup_mod.snapshot(settings.db_path, settings.backup_dir)
    except backup_mod.BackupError as exc:
        # Rule 5 shape: the reason, not a traceback. A scheduled run lands this in
        # data/backup.err, where a stack trace would bury the one useful line.
        typer.echo(f"backup failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        typer.echo(f"backup failed: cannot write to {settings.backup_dir} ({exc})", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"snapshot {path}")
    removed = backup_mod.rotate(settings.backup_dir)
    if removed:
        typer.echo(f"rotated out {len(removed)} older snapshot(s)")


@app.command()
def restore(
    snapshot: Annotated[Path, typer.Argument(help="A backglass-*.db snapshot file")],
    yes: Annotated[
        bool, typer.Option("--yes", help="Actually swap. Without it, nothing is written.")
    ] = False,
) -> None:
    """Replace the live database with a snapshot. Destructive; needs `--yes`.

    Three guards, in order: the snapshot must pass its own integrity check, the current
    database is snapshotted first so the restore itself is reversible, and without
    `--yes` this prints the plan and writes nothing.
    """
    from backglass import backup as backup_mod

    settings = get_settings()
    if not snapshot.exists():
        typer.echo(f"no such snapshot: {snapshot}", err=True)
        raise typer.Exit(code=1)
    if not backup_mod.verify(snapshot):
        typer.echo(
            f"refusing to restore: {snapshot} is not a readable SQLite database or "
            "fails PRAGMA integrity_check",
            err=True,
        )
        raise typer.Exit(code=1)
    if not backup_mod.is_ledger(snapshot):
        typer.echo(
            f"refusing to restore: {snapshot} is a valid SQLite database but not a "
            "Backglass ledger (no schema_version/source_item/commitment tables)",
            err=True,
        )
        raise typer.Exit(code=1)

    if not yes:
        typer.echo(f"would restore {snapshot}")
        typer.echo(f"          onto {settings.db_path}")
        if settings.db_path.exists():
            typer.echo(f"  after snapshotting the current database into {settings.backup_dir}")
        typer.echo("nothing written — rerun with --yes")
        return

    if settings.db_path.exists():
        try:
            safety = backup_mod.snapshot(settings.db_path, settings.backup_dir)
        except (backup_mod.BackupError, OSError) as exc:
            typer.echo(f"refusing to restore: cannot snapshot the current database ({exc})",
                       err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"current database saved to {safety}")

    try:
        backup_mod.restore_into(snapshot, settings.db_path)
    except OSError as exc:
        typer.echo(f"restore failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"restored {settings.db_path} from {snapshot}")


# ── Phase 8: doctor ───────────────────────────────────────────────────────


#: The exact labels launchd/templates/*.plist.tmpl declare. Checked as whole labels,
#: not a substring: the Phase 8 verifier caught the substring version passing on the
#: desktop app's transient GUI registration (application.com.backglass.desktop…)
#: while failing on the real com.backglass.* jobs — wrong both ways.
LAUNCHD_LABELS = (
    "com.backglass.sync",
    "com.backglass.brief",
    "com.backglass.plan",
    "com.backglass.plan-catchup",
    "com.backglass.shutdown",
    "com.backglass.backup",
)


def missing_launchd_jobs(launchctl_list_output: str) -> list[str]:
    loaded = {
        line.split()[-1]
        for line in launchctl_list_output.splitlines()
        if line.strip()
    }
    return [label for label in LAUNCHD_LABELS if label not in loaded]


def _unauthed_remote_sources(
    settings: Settings, known: set[str]
) -> list[tuple[str, str]]:
    """Sources the owner has configured but never authed, and the step each still needs.

    A source with no credential row is invisible everywhere else — the health loop
    iterates credentials and the Sources panel renders them, so a Gmail account listed
    in `.env` that never completed OAuth reads as if it were never asked for. That is
    the difference between "widened intake" and "believed I widened intake."

    The remedy printed with each one is the command as `auth` actually takes it — a bare
    label plus `--source`, the spelling detect.py already prints. `auth gmail:personal`
    stores under `gmail:gmail:personal`, which no connector ever loads, so the next
    doctor run would print the identical line and the "run doctor, do what it says, run
    doctor again" loop would never converge.

    The names are the ones connectors register under, `source:label`, not the bare kind:
    a token source whose credential row reads `canvas:canvas` never matches a `canvas`
    entry, so a working source would be reported as never authed forever.
    """
    from backglass.connectors.canvas import CanvasConnector
    from backglass.connectors.github import GithubConnector
    from backglass.connectors.slack import SlackConnector

    sync_first = "run `backglass sync` once to record the credential"
    wanted: list[tuple[str, str]] = []
    for kind, labels in (
        ("gmail", settings.gmail_accounts),
        ("calendar", settings.calendar_accounts),
        ("drive", settings.drive_accounts),
    ):
        for label in labels:
            wanted.append(
                (f"{kind}:{label}", f"run `backglass auth {label} --source {kind}`")
            )
    if settings.canvas_base_url and settings.canvas_token:
        wanted.append((f"canvas:{CanvasConnector.label}", sync_first))
    if settings.github_token:
        wanted.append((f"github:{GithubConnector.label}", sync_first))
    if settings.slack_token and settings.slack_channels:
        wanted.append((f"slack:{SlackConnector.label}", sync_first))
    return [(source, needs) for source, needs in wanted if source not in known]


#: Sources that can carry another party's mail, files or messages, and therefore the
#: ones docs/08's boundary exists for. A local note store is the owner's own writing;
#: a shared inbox is not.
BOUNDARY_SCOPED = ("gmail", "drive", "slack", "github", "calendar")

#: Spellings a hand import might use for a scoped source. Not exhaustive and cannot be —
#: which is the argument for the allowlist being the boundary and this being a second
#: net, not the first one.
_SOURCE_ALIASES = {
    "gcal": "calendar",
    "google-calendar": "calendar",
    "googlecalendar": "calendar",
    "google-drive": "drive",
    "gdrive": "drive",
    "mail": "gmail",
    "google-mail": "gmail",
}


def _boundary_verdict(
    settings: Settings,
    credentials: Sequence[tuple[str, bool]],
    ingested: Sequence[str] = (),
) -> tuple[bool, str] | None:
    """`(ok, detail)` for the boundary check, or None when it does not apply yet.

    The machinery in connectors/boundary.py is correct and well-tested, but its default
    is inert: `exclude` with an empty denylist excludes nothing while reading as a
    deliberate setting. docs/08 has two legitimate answers — a populated denylist, or
    `full_scope` with the obligations that carries — and "never decided" is neither.
    So this stays silent until a source that can actually carry client data is live,
    and then it fails until the owner has chosen. Rule 6 is the one with legal weight.

    `ingested` is the set of sources that have rows in the ledger, and it is not
    redundant with `credentials`. The first version of this check derived "live" from
    credential rows alone and was therefore silent on a ledger already holding 200
    `calendar:*` items imported without one — boundary-scoped third-party data, present,
    unexamined, and reported as nothing to decide. Data that is already in the ledger is
    the strongest possible reason to have decided, so it counts even when no connector
    claims it.
    """
    def _family(source: str) -> str:
        # Normalized, because the rows this has to catch are hand imports and a hand
        # import picks its own spelling: `Calendar:WORK`, `  calendar:work`, `GCAL:work`.
        # An unnormalized comparison silently passed every one of those — the same
        # blind spot in a new coat.
        head = source.strip().lower().split(":")[0].strip()
        return _SOURCE_ALIASES.get(head, head)

    scoped = sorted(
        {
            _family(source)
            for source, enabled in credentials
            if enabled and _family(source) in BOUNDARY_SCOPED
        }
        | {source for src in ingested if (source := _family(src)) in BOUNDARY_SCOPED}
    )
    if not scoped:
        return None
    decided = (
        Boundary.from_settings(settings).enforcing
        or settings.boundary_mode == "full_scope"
        # docs/08 §The decision as made: a denylist of correspondents is empty when the
        # out-of-scope mail is not in a connected account at all. That is a decision, not
        # an omission — but only when the accounts it excludes are named, because naming
        # them is what makes the connector enforce it and what makes a newly connected
        # mailbox fail loudly instead of quietly ingesting.
        or bool(settings.boundary_out_of_scope_accounts)
    )
    return decided, (
        f"{', '.join(scoped)} enabled with BOUNDARY_MODE=exclude and an empty denylist "
        "— excluding nothing. Set BOUNDARY_DENY_DOMAINS / BOUNDARY_DENY_ADDRESSES, name "
        "the mailboxes that are out of scope in BOUNDARY_OUT_OF_SCOPE_ACCOUNTS, or set "
        "BOUNDARY_MODE=full_scope to accept docs/08 Option B's obligations"
    )


@app.command()
def doctor() -> None:
    """Preflight for activation: one line per check, non-zero exit on any failure.

    Run this after every setup step; a clean doctor is the automation half of
    "activated" — the product half is the seven-day soak.
    """
    import shutil
    import subprocess

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        mark = "ok " if ok else "FAIL"
        if not ok:
            failures += 1
        typer.echo(f"[{mark}] {label}" + (f" — {detail}" if detail and not ok else ""))

    # ── config ────────────────────────────────────────────────────────────
    check("owner emails configured", bool(settings.owner_emails),
          "set OWNER_EMAILS in .env")
    check("brief recipient configured", bool(settings.brief_to),
          "set BRIEF_TO (and RESEND_API_KEY) for delivery")
    # The knowledge base restates two values the pipeline actually runs on. They agree
    # today and nothing would notice if they stopped — see facts.config_drift.
    from backglass import facts as facts_mod

    drift = facts_mod.config_drift(conn, settings)
    check("owner facts agree with config", not drift, "; ".join(drift))
    # Google is one way to reach mail and calendar, and on a Mac that syncs both locally
    # it is not the only one — `apple-mail` and `calendar:apple` read the same accounts
    # with no OAuth client at all. Failing here regardless left the doctor permanently
    # red over a credential the owner had deliberately decided not to obtain, and a check
    # that can never go green is a check people learn to skim past.
    google_ready = bool(settings.google_client_id and settings.google_client_secret)
    local_equivalent = bool(settings.apple_mail_path) and settings.apple_calendar
    if google_ready or not local_equivalent:
        check("google oauth client configured", google_ready,
              "set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET — or read the same accounts "
              "locally with APPLE_MAIL_PATH and APPLE_CALENDAR=1, which need neither")
    else:
        typer.echo(
            "[ -- ] google oauth not configured — mail and calendar are read locally "
            "through Mail.app and Calendar.app instead (docs/07)"
        )

    # ── batch mode (informational — never a failure; the plists are optional) ──
    from datetime import UTC as _utc
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    stuck_cutoff = (_dt.now(_utc) - _td(hours=26)).isoformat()
    stuck = conn.execute(
        "SELECT COUNT(*) AS n FROM model_batch"
        " WHERE user_id = ? AND status = 'submitted' AND created_at < ?",
        (USER_ID, stuck_cutoff),
    ).fetchone()
    if stuck and stuck["n"]:
        typer.echo(
            f"[note] {stuck['n']} batch(es) submitted >26h ago — "
            "run `backglass batch collect` (items already fall back to sync)"
        )

    # ── model access ──────────────────────────────────────────────────────
    if settings.model_backend == "claude_cli":
        try:
            path = model_client._find_claude()
            check("claude CLI reachable", True)
            del path
        except model_client.ModelError as exc:
            check("claude CLI reachable", False, str(exc))
    elif settings.model_backend == "anthropic":
        check("anthropic key configured",
              bool(model_client.anthropic_api_key(settings)),
              "MODEL_BACKEND=anthropic needs MODEL_API_KEY or ANTHROPIC_API_KEY")
    else:
        check("deepinfra key configured", bool(settings.model_api_key),
              "MODEL_BACKEND=deepinfra needs MODEL_API_KEY")

    if settings.apple_triage:
        listed = ""
        if shutil.which("shortcuts"):
            done = subprocess.run(["shortcuts", "list"], capture_output=True, text=True)
            listed = done.stdout
        check(f"shortcut '{settings.apple_triage_shortcut}' exists",
              settings.apple_triage_shortcut in listed,
              "create it in Shortcuts.app (Receive Text → Use Model → Stop and output)")

    # ── credentials + connector health ────────────────────────────────────
    rows = list(conn.execute(
        "SELECT source, status, enabled FROM credential WHERE user_id = ?", (USER_ID,)
    ))
    check("at least one source has synced or authed", bool(rows),
          "run `backglass auth <label>` then `backglass sync --dry-run`")
    for row in rows:
        if not row["enabled"]:
            typer.echo(f"[ -- ] {row['source']} — paused by owner")
            continue
        check(f"credential {row['source']} healthy", row["status"] == "ok",
              str(row["status"]))

    contacts_source = _contacts_source(conn, settings)
    for connector in [*_all_connectors(conn, settings), *filter(None, [contacts_source])]:
        health = connector.health()
        check(f"connector {connector.name}", health.ok, health.detail or "")
        _check_reconciles(conn, connector, check)

    # Informational, never failures: stores this machine has that one
    # `backglass setup` run would hook up (Phase A2).
    from backglass.connectors import detect as detect_mod

    authed = {str(r["source"]) for r in rows if r["status"] == "ok"}
    for d in detect_mod.detect_all(settings, authed=authed):
        if d.status == detect_mod.FOUND:
            typer.echo(f"[ -- ] {d.source} detected but not configured — `backglass setup`")

    # Informational: connectors that exist in code and are configured to run but have
    # never authed. Distinct from the detection lines above — those are stores this Mac
    # *has*, these are sources the owner has *asked for* and never finished connecting,
    # which is otherwise invisible: a source that never authed has no credential row, so
    # neither the health loop nor the Sources panel says anything about it at all.
    for label, needs in _unauthed_remote_sources(settings, {str(r["source"]) for r in rows}):
        typer.echo(f"[ -- ] {label} configured but never authed — {needs}")

    # ── timezone config ───────────────────────────────────────────────────
    # Validated once, here, rather than defended against in every function that reads
    # it: a bad zone name in TZ_RANGES is inert until the stay begins and then moves the
    # working window, the brief's delivery time and every day-boundary query at once.
    from backglass.plan import timezones as tz_mod

    for problem in tz_mod.zone_problems(settings):
        check("timezone config", False, problem)

    # ── data boundary (docs/08, CLAUDE.md rule 6 — legal weight) ──────────
    verdict = _boundary_verdict(
        settings,
        [(str(r["source"]), bool(r["enabled"])) for r in rows],
        ingested=[
            str(r["source"])
            for r in conn.execute(
                "SELECT DISTINCT source FROM source_item WHERE user_id = ?", (USER_ID,)
            )
        ],
    )
    if verdict is not None:
        check("data boundary decided", *verdict)

    # Which mailboxes the ledger has actually read. docs/08's decision rests on the
    # account list, so the account list is printed rather than assumed — a mailbox added
    # to Mail.app months from now shows up here on the next sync, which is the only way
    # the owner finds out that the decision needs making again.
    accounts = [
        str(r["account"])
        for r in conn.execute(
            "SELECT DISTINCT json_extract(raw_json, '$.delivered_to') AS account"
            " FROM source_item WHERE user_id = ? AND source = 'apple-mail'"
            " AND json_extract(raw_json, '$.delivered_to') != ''"
            " ORDER BY account",
            (USER_ID,),
        )
        if r["account"]
    ]
    if accounts:
        typer.echo(f"[ -- ] mail accounts read: {', '.join(accounts)}")
        out_of_scope = {a.strip().lower() for a in settings.boundary_out_of_scope_accounts}
        breached = sorted(set(accounts) & out_of_scope)
        if breached:
            check(
                "no out-of-scope mailbox ingested",
                False,
                f"{', '.join(breached)} is named in BOUNDARY_OUT_OF_SCOPE_ACCOUNTS but "
                "its mail is in the ledger — run `backglass purge-boundary` and re-read "
                "docs/08 before the next sync",
            )

    # ── scheduling ────────────────────────────────────────────────────────
    done = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    missing = missing_launchd_jobs(done.stdout)
    check("launchd jobs loaded", not missing,
          f"missing {', '.join(missing)} — run `backglass schedule install`; "
          "without them nothing runs at 05:45/06:00")

    # ── the scheduler is loaded; is it actually running? (heartbeat) ──────
    # A loaded job that throws on every fire looks identical to a healthy one in
    # `launchctl list`. The run table is the only witness. Stale only fails when the
    # sync job is loaded — otherwise the missing-job check above is already the red,
    # and two reds for one cause teach skimming.
    from backglass import heartbeat as heartbeat_mod

    beat = heartbeat_mod.read(conn, settings, _today(settings))
    if beat.never_ran:
        typer.echo("[note] sync has never run — `backglass sync` once to prove the pipeline")
    elif "com.backglass.sync" not in missing:
        check(
            "sync is running on schedule",
            not beat.stale,
            f"last run {beat.age_phrase} (threshold {settings.sync_stale_after_hours}h) — "
            "the job is loaded but not producing runs; check data/sync.err",
        )

    # ── backups (audit #23) ───────────────────────────────────────────────
    from backglass import backup as backup_mod

    level, detail = backup_mod.freshness(
        settings.backup_dir, job_installed="com.backglass.backup" not in missing
    )
    if level == "note":
        typer.echo(f"[note] {detail}")
    else:
        check("ledger backup is fresh", level == "ok", detail)

    typer.echo("all clear" if not failures else f"{failures} check(s) failing")
    raise typer.Exit(code=1 if failures else 0)


schedule_app = typer.Typer(
    help="Render and install the launchd jobs. docs/10 §Scheduling."
)
app.add_typer(schedule_app, name="schedule")


@schedule_app.command("install")
def schedule_install(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Render the plists and print them; write nothing."),
    ] = False,
) -> None:
    """Render launchd/templates/*.plist.tmpl for this machine and load them.

    Writes to ~/Library/LaunchAgents and runs `launchctl load` on each. Safe to run
    again after moving the repo or reinstalling uv — it just re-renders and reloads.
    """
    from backglass import schedule as schedule_mod
    from backglass.extract.client import anthropic_api_key

    batch_lane = bool(anthropic_api_key(get_settings()))
    try:
        rendered = schedule_mod.install(dry_run=dry_run, batch_lane=batch_lane)
    except schedule_mod.ScheduleError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if dry_run:
        for filename, text in rendered.items():
            typer.echo(f"── {filename} " + "─" * max(0, 60 - len(filename)))
            typer.echo(text)
        return

    for filename in rendered:
        typer.echo(f"installed {filename}")
    if not batch_lane:
        typer.echo(
            "batch jobs skipped: no Anthropic API key, so batch submit/collect can "
            "only fail — the sync path covers extraction. Set MODEL_API_KEY or "
            "ANTHROPIC_API_KEY and re-run to schedule the overnight lane."
        )
    typer.echo(f"wrote {len(rendered)} job(s) to {schedule_mod.LAUNCH_AGENTS_DIR}")
    typer.echo("verify with `backglass doctor` or `launchctl list | grep backglass`")


memory_app = typer.Typer(
    invoke_without_command=True,
    help="The personal knowledge base: durable facts with provenance.",
)
app.add_typer(memory_app, name="memory")


@memory_app.callback()
def memory_list(ctx: typer.Context) -> None:
    """List active facts, grouped by subject. Same read the Memory page runs."""
    if ctx.invoked_subcommand is not None:
        return
    from backglass import facts

    conn = _open(get_settings())
    migrate(conn)
    current = None
    rows = facts.recall(conn)
    waiting = len(facts.proposed(conn))
    if waiting:
        typer.echo(f"{waiting} proposed fact(s) waiting — `backglass memory proposed`")
    if not rows:
        typer.echo("nothing remembered yet")
        return
    for f in rows:
        if f.subject != current:
            current = f.subject
            typer.echo(f"\n[{current}]")
        note = f"  — {f.note}" if f.note else ""
        stamp = f"({f.source} · {f.created_at[:10]})"
        typer.echo(f"  #{f.fact_id} {f.key}: {f.value}{note}  {stamp}")


@memory_app.command("set")
def memory_set(
    subject: str,
    key: str,
    value: str,
    note: Annotated[str | None, typer.Option("--note", help="Evidence, in words")] = None,
    from_item: Annotated[
        int | None,
        typer.Option("--from", help="Source item id this fact was learned from"),
    ] = None,
) -> None:
    """Remember a fact. The previous value for this subject+key is superseded, not lost.

    `--from` records which source item taught it. `fact.source_item_id` has existed
    since the table did, `/source/{id}` already renders "what came of this item" from
    it, and every one of the owner's twenty facts has it NULL — not because nobody
    bothered, but because this command was the only writer and had no way to pass one.
    A column no door can reach is a column that will read as unused and get dropped.
    """
    from backglass import facts

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    if from_item is not None:
        known = conn.execute(
            "SELECT 1 FROM source_item WHERE id = ? AND user_id = ?", (from_item, USER_ID)
        ).fetchone()
        if known is None:
            # Refused here rather than left to the foreign key, because the FK message
            # names a constraint and this names the mistake.
            typer.echo(f"no source item {from_item}", err=True)
            raise typer.Exit(code=1)
    try:
        fact_id = facts.remember(
            conn, settings, subject, key, value, note=note, source_item_id=from_item
        )
    except facts.FactError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    conn.commit()
    typer.echo(f"fact {fact_id}: {subject}/{key} = {value}")


@memory_app.command("forget")
def memory_forget(fact_id: int) -> None:
    """Retract a fact. The row survives; only its claim is withdrawn."""
    from backglass import facts

    conn = _open(get_settings())
    migrate(conn)
    try:
        facts.forget(conn, fact_id)
    except facts.FactError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    conn.commit()
    typer.echo(f"fact {fact_id} retracted")


@memory_app.command("proposed")
def memory_proposed() -> None:
    """Fact candidates the pipeline extracted but may not write on its own.

    Nothing here reaches owner_context or any model call until accepted — the poison
    gate for memory. Accept with `backglass memory accept <id>`, discard with
    `backglass memory reject <id>`.
    """
    from backglass import facts

    conn = _open(get_settings())
    migrate(conn)
    rows = facts.proposed(conn)
    if not rows:
        typer.echo("nothing proposed — the pipeline has no facts waiting on you")
        return
    for f in rows:
        conf = f"conf {f.confidence:.2f}" if f.confidence is not None else "no confidence"
        src = f" · source item #{f.source_item_id}" if f.source_item_id else ""
        typer.echo(f"  #{f.fact_id} {f.subject}/{f.key}: {f.value}  ({conf}{src})")
        if f.note:
            typer.echo(f'      "{f.note}"')


@memory_app.command("accept")
def memory_accept(fact_id: int) -> None:
    """Accept a proposed fact into active memory (supersedes any current value)."""
    from backglass import facts

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    try:
        new_id = facts.accept(conn, settings, fact_id)
    except facts.FactError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"accepted as fact #{new_id}")


@memory_app.command("reject")
def memory_reject(fact_id: int) -> None:
    """Discard a proposed fact. It keeps its row (retracted), and a later message
    restating it may propose it again — wrong in June can be true in September."""
    from backglass import facts

    conn = _open(get_settings())
    migrate(conn)
    try:
        facts.reject(conn, fact_id)
    except facts.FactError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo("rejected")


@memory_app.command("export")
def memory_export(
    out: Annotated[Path | None, typer.Option("--out", help="Write to a file")] = None,
) -> None:
    """The whole active memory as markdown — what an assistant loads instead of
    searching mail."""
    from backglass import facts

    conn = _open(get_settings())
    migrate(conn)
    doc = facts.export_markdown(conn)
    if out:
        out.write_text(doc)
        typer.echo(f"wrote {out}")
    else:
        typer.echo(doc)


@memory_app.command("context")
def memory_context() -> None:
    """The assembled block every model call now carries: facts, people, situation.

    Exactly what backglass/context.py hands triage and extraction — printed so the
    owner can read what the models are being told about them, and so a wrong line can
    be traced to its table (facts → `memory`, people → the entity ledger, situation →
    the board) instead of guessed about.
    """
    from backglass import context as context_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    block = context_mod.assemble(conn, settings)
    typer.echo(block if block else "(empty — the ledger has no facts, people or open items)")


@app.command("notifications")
def notifications_command(
    limit: Annotated[int, typer.Option("--limit", help="How many to show")] = 20,
) -> None:
    """What the system has told the owner, newest first — the notification ledger.

    Delivery is best-effort osascript; this table is the record that survives it.
    """
    from backglass import notify as notify_mod

    conn = _open(get_settings())
    migrate(conn)
    rows = notify_mod.recent(conn, limit=limit)
    if not rows:
        typer.echo("nothing sent yet")
        return
    for r in rows:
        typer.echo(f"  {r['local_date']} [{r['kind']}] {r['title']} — {r['body']}"
                   f"  ({r['delivered']})")


decisions_app = typer.Typer(
    invoke_without_command=True,
    help="Major decisions: the choices the owner has settled.",
)
app.add_typer(decisions_app, name="decisions")


@decisions_app.callback(invoke_without_command=True)
def decisions_list(
    ctx: typer.Context,
    disposals: Annotated[
        bool,
        typer.Option(
            "--disposals", help="What the logic checker threw out, not what you decided"
        ),
    ] = False,
) -> None:
    """List standing decisions, newest first. Same read the Decisions page runs.

    `--disposals` is the page's lower half: the logic checker's own work, which is
    recorded in the same table and deliberately kept out of the standing list. A guard on
    one door is not a guard, and neither is a surface — what the machine threw out has to
    be readable from here too.
    """
    if ctx.invoked_subcommand is not None:
        return
    from backglass import decisions

    conn = _open(get_settings())
    migrate(conn)
    if disposals:
        thrown = decisions.disposals(conn)
        if not thrown:
            typer.echo("the logic checker has disposed of nothing")
            return
        for d in thrown:
            typer.echo(
                f"  #{d.decision_id} [{d.decided_at[:10]}] "
                f"{d.commitment_title or d.title}: {d.choice} — {d.reasoning}"
            )
        return
    rows = decisions.active(conn)
    if not rows:
        typer.echo("no decisions recorded yet")
        return
    for d in rows:
        why = f"  — {d.reasoning}" if d.reasoning else ""
        mark = "closed" if d.closed_commitment else "re"
        closed = f"  ({mark}: {d.commitment_title})" if d.commitment_title else ""
        typer.echo(
            f"  #{d.decision_id} [{d.decided_at[:10]}] {d.title}: {d.choice}{why}{closed}"
        )


@decisions_app.command("record")
def decisions_record(
    title: str,
    choice: str,
    why: Annotated[str | None, typer.Option("--why", help="Reasoning, in words")] = None,
    closes: Annotated[
        int | None,
        typer.Option("--closes", help="Open commitment this decision settles (dropped)"),
    ] = None,
) -> None:
    """Record a decision. The previous decision with this title is superseded, not lost;
    an open commitment named by --closes is dropped in the same transaction."""
    from backglass import decisions

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    try:
        decision_id, closed = decisions.record(
            conn, settings, title, choice, reasoning=why, commitment_id=closes
        )
    except decisions.DecisionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    conn.commit()
    tail = f"; closed commitment {closes}" if closed else ""
    typer.echo(f"decision {decision_id}: {title} — {choice}{tail}")


@decisions_app.command("revisit")
def decisions_revisit(decision_id: int) -> None:
    """Withdraw a decision. The row survives; a commitment it closed stays closed."""
    from backglass import decisions

    conn = _open(get_settings())
    migrate(conn)
    try:
        decisions.revisit(conn, decision_id)
    except decisions.DecisionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    conn.commit()
    typer.echo(f"decision {decision_id} retracted")


if __name__ == "__main__":
    main()


#: Two explicit subcommands rather than a bare positional query. With
#: `invoke_without_command` and an optional argument on the callback, `search index` is
#: parsed as a search *for* the word "index" and the flags after it become an unknown
#: command — a parser ambiguity that reads to the owner as a broken CLI.
search_app = typer.Typer(
    help="Find a document by what it was about. Owner's ruling 2026-08-10; docs/02.",
)
app.add_typer(search_app, name="search")


@search_app.command("find")
def search_run(
    query: Annotated[str | None, typer.Argument(help="What the document was about")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 10,
) -> None:
    """Rank kept documents against a question, best first.

    Reports coverage on every run. A search over a tenth of the ledger looks exactly like
    a search over all of it right up until the answer is the part that was missing, so the
    proportion is stated rather than left to be inferred from results that looked fine.
    """
    from backglass import search as search_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    stats = search_mod.coverage(conn, settings)
    if not query:
        typer.echo(
            f"{stats['indexed']} of {stats['indexable']} documents indexed "
            f"({stats['model']}); {stats['pending']} pending"
        )
        typer.echo('run `backglass search index`, then `backglass search find "..."`')
        return
    if stats["indexed"] == 0:
        typer.echo("nothing indexed yet — run `backglass search index`", err=True)
        raise typer.Exit(code=1)

    try:
        hits = search_mod.search(conn, settings, query, limit=limit)
    except search_mod.SearchError as exc:
        typer.echo(f"search failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    for hit in hits:
        # The fold is stated, never silent. "×8 since 17 Apr" is a fact about the corpus
        # the owner cannot get anywhere else, and hiding seven rows without saying so is
        # the same silent truncation P2 forbids one panel over.
        repeats = (
            f"  ×{hit.copies} since {hit.first_seen[:10]}" if hit.copies > 1 else ""
        )
        typer.echo(
            f"  {hit.score:.3f}  {hit.occurred_at[:10]}  {hit.title[:56]}"
            f"   [{hit.source} #{hit.source_item_id}]{repeats}"
        )
    if stats["pending"]:
        # Not a warning about the results shown; a statement about the ones that could not
        # be. Silence here is how a partial index reads as a complete answer.
        typer.echo(
            f"  · {stats['pending']} document(s) not indexed and therefore not searched",
            err=True,
        )


@search_app.command("index")
def search_index(
    limit: Annotated[int, typer.Option("--limit", help="Documents per run")] = 200,
    all_pending: Annotated[
        bool, typer.Option("--all", help="Keep going until nothing is pending")
    ] = False,
    kind: Annotated[
        str,
        typer.Option(
            "--kind",
            help="source_item (searchable documents) or commitment (feeds duplicate detection)",
        ),
    ] = "source_item",
) -> None:
    """Embed kept documents that have no vector yet.

    Resumable by construction — the unique index is the watermark — so interrupting this
    costs nothing and re-running continues rather than restarting.
    """
    from backglass import search as search_mod

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)

    total = 0
    while True:
        try:
            added = search_mod.index(conn, settings, limit=limit, kind=kind)
        except search_mod.SearchError as exc:
            conn.commit()
            typer.echo(f"indexing stopped after {total}: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        conn.commit()
        total += added
        if added:
            typer.echo(f"indexed {total}…")
        if not added or not all_pending:
            break

    stats = search_mod.coverage(conn, settings, kind)
    typer.echo(
        f"{stats['indexed']} of {stats['indexable']} {stats['kind']}s indexed "
        f"({stats['model']}); {stats['pending']} pending"
    )
