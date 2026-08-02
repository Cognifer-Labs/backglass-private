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
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any

import typer

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
from backglass.sync import EXTRACT_PROMPT, sync

app = typer.Typer(
    add_completion=False,
    help="Backglass — a commitment ledger with documents as evidence.",
    no_args_is_help=True,
)


def _open(settings: Settings) -> sqlite3.Connection:
    return connect(settings.db_path)


def _build_model_client(settings: Settings) -> Any:
    """`model_client.build()`, but a missing key/CLI degrades (Rule 5) instead of an
    uncaught `ModelError` producing a full stack trace for a fresh, unconfigured clone."""
    try:
        return model_client.build(settings)
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
    migrate(conn)
    connectors = _all_connectors(conn, settings)
    if not connectors:
        typer.echo("no sources configured; run `backglass auth <label>` first", err=True)
        raise typer.Exit(2)

    report = sync(conn, settings, connectors, _build_model_client(settings), dry_run=dry_run)
    _print_report(report, dry_run=dry_run)
    raise typer.Exit(report.exit_code)


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
        typer.echo(
            f"\nlast run  {last['started_at']}  fetched {last['items_fetched']}, "
            f"extracted {last['items_extracted']}, writes {last['writes']}, "
            f"spend {last['spend_cents']}c" + ("  DEGRADED" if last["degraded"] else "")
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
        f"{verb} {report.source_items} source item(s), {report.commitments} commitment(s)"
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
    from datetime import date as _date

    from backglass.goals import health
    from backglass.plan import planner, timezones

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    day = _date.fromisoformat(for_date) if for_date else _today(settings)

    if if_missing and planner.current_plan_id(conn, day) is not None:
        typer.echo(f"{day}: already planned — nothing to do")
        return

    proposal = planner.propose(
        conn, settings, day, at_risk_goals=health.at_risk_goal_ids(conn, settings, day)
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
        mark = {"protected": " [protected]", "fixed": " [fixed]", "small": " [small]"}.get(
            str(block["kind"]), ""
        )
        start = str(block["starts_at"])[11:16]
        end = str(block["ends_at"])[11:16]
        typer.echo(f"  {start}–{end}  {block['title']}{mark}")
    for note in proposal.notes:
        typer.echo(f"  · {note}")
    if proposal.overflow:
        for item in proposal.overflow:
            typer.echo(f"  · did not fit: {item.what} ({item.minutes}m)", err=True)
    del timezones


@app.command()
def shutdown(
    for_date: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD")] = None,
    done: Annotated[
        str | None, typer.Option("--done", help="Comma-separated block ids")
    ] = None,
    learned: Annotated[str | None, typer.Option("--learned")] = None,
    blocked: Annotated[str | None, typer.Option("--blocked")] = None,
) -> None:
    """The evening pass. docs/04 §1.8.

    Optional and skippable. If no `--done` is given, completion is inferred from ledger
    state and the rest rolls over. Nothing here nags about a skipped shutdown — docs/04:
    "A productivity system that scolds gets deleted."
    """
    from datetime import date as _date

    from backglass.brief import weekly
    from backglass.plan import rollover

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    day = _date.fromisoformat(for_date) if for_date else _today(settings)

    ids = {int(part) for part in done.split(",") if part.strip()} if done else None
    report = rollover.close_day(conn, settings, day, done_block_ids=ids)
    if learned or blocked:
        rollover.record_shutdown(conn, day, learned=learned, blocked=blocked)
    made = weekly.link_completed_work(conn)

    typer.echo(f"{day}: {report.done} done, {report.rolled} rolled, {made} checkpoint(s)")
    for row in report.flagged:
        typer.echo(f"  · rolled {row['rollover_count']}x: {row['what']}")


def _today(settings: Settings):  # type: ignore[no-untyped-def]
    from backglass.brief.daily import today_in

    return today_in(settings.default_tz)


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
    from datetime import date as _date

    from backglass.brief import daily, deliver, render

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    target = _date.fromisoformat(for_date) if for_date else daily.today_in(settings.default_tz)

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


def _all_connectors(conn: sqlite3.Connection, settings: Settings) -> list[Connector]:
    """Every configured source. docs/02 §Failure policy: one failing source does not stop
    the others, so an unauthorised connector is skipped rather than fatal."""
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

        built.append(
            IMessageConnector(db_path=settings.imessage_db_path, boundary=boundary)
        )

    if settings.instagram_chats and (
        settings.instagram_export_path
        or (settings.instagram_username and settings.instagram_session_file)
    ):
        from backglass.connectors.instagram import (
            Allowlist,
            InstagramExportConnector,
            InstagramLiveConnector,
        )

        allowlist = Allowlist(settings.instagram_chats)
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
    typer.echo(f"  writes {report.writes}, spend {report.spend_cents}c")
    if report.degraded:
        typer.echo("  DEGRADED: spend cap reached, extraction skipped", err=True)
    for rule, count in sorted(report.excluded_by_rule.items()):
        typer.echo(f"  boundary excluded {count} by rule {rule}")
    for note in report.date_notes:
        typer.echo(f"  date: {note}", err=True)
    for error in report.errors:
        typer.echo(f"  error: {error}", err=True)


def main() -> None:
    sys.exit(app())


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
) -> None:
    """Quick-add a commitment by hand. Provenance is a manual source item."""
    from backglass.web import actions

    if owe == owed:
        typer.echo("pass exactly one of --owe / --owed", err=True)
        raise typer.Exit(code=1)
    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    cid = actions.quick_add(
        conn,
        settings,
        what=what,
        direction="i_owe" if owe else "owed_to_me",
        counterparty=who,
        due_at=due,
        minutes=minutes,
    )
    typer.echo(f"commitment {cid} added")


# ── Phase 6: roadmaps ─────────────────────────────────────────────────────

roadmap_app = typer.Typer(help="Preset career-path roadmaps over the goal engine.")
app.add_typer(roadmap_app, name="roadmap")

goals_app = typer.Typer(help="Goal targets beyond what the dashboard edits.")
app.add_typer(goals_app, name="goals")


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
    from datetime import date as _date

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
        # it runs and the commit below is a formality — a failure after it left an
        # activity with no hours, and the natural retry (`--new` again) made a second
        # one, then a third, until the plain name was permanently ambiguous and one
        # activity's hours were split across duplicate AMCAS rows. That is exactly the
        # unrecoverable mis-filing this command exists to prevent.
        if activities_mod.find_by_name(conn, activity):
            typer.echo(
                f"an activity named {activity!r} already exists — log against it "
                "without --new, or pick a distinct title",
                err=True,
            )
            raise typer.Exit(code=1)
        if activities_mod.total_target_for(conn, new) is None:
            typer.echo(
                f"category {new!r} has no lifetime hour target on any active goal, so "
                f"there is nowhere to log {hours}h — nothing was created",
                err=True,
            )
            raise typer.Exit(code=1)
        try:
            activities_mod.check_hours(hours)
        except activities_mod.ActivityError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
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

    # A backdated entry is stamped local noon on the day named; a same-day entry keeps
    # the actual moment. local_now_iso takes a day only to pick the zone — it always
    # stamps now — so using it for --on would file September's hours under today.
    today = _today(settings)
    if on:
        try:
            day = _date.fromisoformat(on)
        except ValueError:
            typer.echo(f"--on {on!r} is not a date; use YYYY-MM-DD", err=True)
            raise typer.Exit(code=1) from None
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
    from datetime import date as _date

    from backglass.roadmap import instantiate, interview, presets

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    try:
        preset = presets.load(path)
    except presets.PresetError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    start_date = _date.fromisoformat(start) if start else _today(settings)

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
    from datetime import date as _date

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
            adjust.redate_step(conn, step_id, _date.fromisoformat(on_date))
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
        int, typer.Option("--min-evidence", help="Model drops required")
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
        "candidates (zero keeps ever, no commitment ever; a sender's first real"
        " ask after promotion would be lost — promote deliberately):"
    )
    for c in found:
        window = f"{(c.first_seen or '')[:10]}..{(c.last_seen or '')[:10]}"
        typer.echo(
            f"  {c.kind:<7} {c.value:<36} {c.evidence_count:>3} model drops  {window}"
            f"  e.g. {c.sample_reason or '—'}"
        )
    typer.echo("promote with: backglass noise promote <value> | --all")


@noise_app.command("promote")
def noise_promote(
    value: Annotated[str | None, typer.Argument(help="Address or domain")] = None,
    all_: Annotated[
        bool, typer.Option("--all", help="Promote every address candidate")
    ] = False,
    min_evidence: Annotated[int, typer.Option("--min-evidence")] = 5,
) -> None:
    """Promote candidates into free tier-0 drops. Refuses any sender with a keep."""
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
    if m.degraded_runs:
        typer.echo(
            "  ← spend cap was reached this month; extraction has been skipped", err=True
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
        flags = ("degraded" if r["degraded"] else "") + (" errors" if r["had_errors"] else "")
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
    report = batch_mod.submit(
        conn, settings, connectors, model_client.build(settings)
    )
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
    report = batch_mod.collect(conn, settings)
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
    for error in report.errors:
        typer.echo(f"  {error}", err=True)
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
    """
    wanted: list[tuple[str, str]] = []
    for label in settings.gmail_accounts:
        wanted.append((f"gmail:{label}", f"run `backglass auth gmail:{label}`"))
    for label in settings.calendar_accounts:
        wanted.append((f"calendar:{label}", f"run `backglass auth calendar:{label}`"))
    for label in settings.drive_accounts:
        wanted.append((f"drive:{label}", f"run `backglass auth drive:{label}`"))
    if settings.canvas_base_url and settings.canvas_token:
        wanted.append(("canvas", "run `backglass sync` once to record the credential"))
    if settings.github_token:
        wanted.append(("github", "run `backglass sync` once to record the credential"))
    if settings.slack_token and settings.slack_channels:
        wanted.append(("slack", "run `backglass sync` once to record the credential"))
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
        # import picks its own spelling: `Calendar:ASU`, `  calendar:asu`, `GCAL:asu`.
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
    )
    return decided, (
        f"{', '.join(scoped)} enabled with BOUNDARY_MODE=exclude and an empty denylist "
        "— excluding nothing. Set BOUNDARY_DENY_DOMAINS / BOUNDARY_DENY_ADDRESSES, or "
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
    check("google oauth client configured",
          bool(settings.google_client_id and settings.google_client_secret),
          "set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET")

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

    for connector in _all_connectors(conn, settings):
        health = connector.health()
        check(f"connector {connector.name}", health.ok, health.detail or "")

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

    try:
        rendered = schedule_mod.install(dry_run=dry_run)
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
) -> None:
    """Remember a fact. The previous value for this subject+key is superseded, not lost."""
    from backglass import facts

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    try:
        fact_id = facts.remember(conn, settings, subject, key, value, note=note)
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


if __name__ == "__main__":
    app()
