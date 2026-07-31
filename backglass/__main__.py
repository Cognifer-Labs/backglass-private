"""The command line. docs/10 §CLI.

The whole system is operated from here and the surface is deliberately small. `--dry-run`
on sync is a hard requirement rather than a convenience: it prints the diff and writes
nothing, and it is what makes every phase after this one debuggable.
"""

from __future__ import annotations

import json
import sqlite3
import sys
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
from backglass.ledger import USER_ID
from backglass.sync import EXTRACT_PROMPT, sync

app = typer.Typer(
    add_completion=False,
    help="Backglass — a commitment ledger with documents as evidence.",
    no_args_is_help=True,
)


def _open(settings: Settings) -> sqlite3.Connection:
    return connect(settings.db_path)


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

    report = sync(conn, settings, connectors, model_client.build(settings), dry_run=dry_run)
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


@app.command()
def dashboard(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8765,
) -> None:
    """Serve the dashboard. docs/06.

    Binds to loopback by default. docs/08: no telemetry leaves the machine, and a page
    rendering the owner's commitments has no business being reachable from the network.
    """
    from backglass.web.app import serve

    settings = get_settings()
    typer.echo(f"http://{host}:{port}  (db: {settings.db_path})")
    serve(settings, host=host, port=port)


@app.command()
def plan(
    for_date: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD")] = None,
    accept: Annotated[
        bool, typer.Option("--accept", help="Mark the proposal accepted")
    ] = False,
) -> None:
    """Propose a day. docs/04 §1.

    A proposal, never an imposition — docs/04 §6 rules out automatic calendar writes, so
    this writes a `day_plan` and nothing else. Regenerating supersedes the prior plan and
    keeps it (§3).
    """
    from datetime import date as _date

    from backglass.goals import health
    from backglass.plan import planner, timezones

    settings = get_settings()
    conn = _open(settings)
    migrate(conn)
    day = _date.fromisoformat(for_date) if for_date else _today(settings)

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


if __name__ == "__main__":
    app()


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
        client = model_client.build(settings)
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


# ── Phase 8: doctor ───────────────────────────────────────────────────────


@app.command()
def doctor() -> None:
    """Preflight for activation: one line per check, non-zero exit on any failure.

    The runbook (docs/13) says run this after every setup step; a clean doctor is
    the automation half of "activated" — the product half is the seven-day soak.
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

    # ── model access ──────────────────────────────────────────────────────
    if settings.model_backend == "claude_cli":
        try:
            path = model_client._find_claude()
            check("claude CLI reachable", True)
            del path
        except model_client.ModelError as exc:
            check("claude CLI reachable", False, str(exc))
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

    # ── scheduling ────────────────────────────────────────────────────────
    done = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    loaded = "com.backglass" in done.stdout
    check("launchd jobs loaded", loaded,
          "install per launchd/README.md — without them nothing runs at 05:45/06:00")

    typer.echo("all clear" if not failures else f"{failures} check(s) failing")
    raise typer.Exit(code=1 if failures else 0)
