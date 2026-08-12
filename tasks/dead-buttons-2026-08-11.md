# Resolve / Snooze / Drop did nothing (2026-08-11)

Reported as "resolve snooze and drop dont work, fix backend to register changes",
clicked in the desktop `Backglass.app`, with **nothing at all** on screen — no row
change, no failed-write strip.

## What was ruled out, and how

| Suspicion | Evidence against it |
|---|---|
| The routes or `actions.py` are broken | `POST /commitments/{id}/snooze/1` from WebKit returned 200 and moved `due_at` 08-12 → 08-13, `rollover_count` 0 → 1 |
| The frozen app serves stale markup | `_board.html`, `base.html` and `htmx.min.js` in `/Applications/Backglass.app` are byte-identical to the checkout; only `person.html` is stale |
| The host/CSRF guards refuse the write | `POST` with a bogus id answered 422 `no commitment 999999999` on the live sidecar; `tailscale serve status` reports no proxy, so no non-loopback name is in play |
| The app talks to a different ledger | its `.env` is a symlink to the repo's, `DB_PATH` absolute |

## What was actually wrong, and is fixed

`connect()` set no `busy_timeout`, so every connection took Python's five-second
default. The sync runs every half hour for one to four minutes and takes a short
`BEGIN IMMEDIATE` per extracted item; a write clicked inside that window queues behind
that stream and could lose the race for longer than five seconds. `sqlite3.OperationalError`
is not `ActionError`, so it left the route as a bare 500 whose body is Starlette's own
plain-text page — which `oops.js` cannot parse, so the strip could only show a number.
The write itself was gone.

- `db.BUSY_TIMEOUT_MS = 30_000`, applied on every connection.
- A `sqlite3.OperationalError` handler on the app turns what is left into a 503 with a
  `detail` sentence, which is the shape the strip reads.
- `tests/test_edges.py::TestAWriteThatLosesTheRaceWithTheSync` holds the lock from a
  second connection and asserts both halves: the write waits and lands, and a writer
  that never lets go produces a sentence rather than a 500.

Reproduced before the fix as 500 after 5.0s with nothing written; after it, 200 after
6.2s with the row moved.

## A third defect, found in the log while driving the app

A dashboard `GET /` answered 200 and then raised out of the dependency teardown:

```
File "backglass/web/app.py", line 63, in get_conn
    conn.close()
sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in
that same thread.
```

FastAPI runs a sync route in a worker thread, and runs the setup and the teardown of a
sync generator dependency as two separate threadpool calls that anyio may schedule on
two different workers. `connect()` took SQLite's default `check_same_thread=True`, so
the connection was opened on one thread and closed on another and the close failed,
leaking it. The teardown is the harmless end: the same scheduling one call earlier
raises inside the route body, which is a 500 with the write lost — a button that does
nothing, intermittently, for a reason nothing on the page can explain. Fixed with
`check_same_thread=False`, which is a handoff and not sharing: one connection, one
request, never two threads at once.

The three regression tests were run against the unfixed line first and one of them
fails there, which is the only reason to trust the other two.

## Shipped

`desktop/build-sidecar.sh` rebuilt the frozen backend and the bundle, and it was
installed over `/Applications/Backglass.app`. Verified behaviourally rather than by a
version string: the rebuilt binary, run against a copy of the real ledger with a second
connection holding the write lock, answered 200 after 6.9s and moved the row —
the same click the old binary lost. `backglass state` now reports `matches_source: True`
with no stale surfaces.

## The actual cause of "Drop does nothing"

Found by driving the real app window: `POST /commitments/343/snooze/1` and
`/105/resolve` both reached the server from the app, and **no drop request ever did** —
zero, across the whole log. The difference between Drop and the other two is one
attribute. `hx-confirm` calls `window.confirm`; the shell's WKWebView implements no
confirm panel, so the call returns false and htmx cancels the request. The click was
answered by nothing at all, which is what was reported and what no server log could
show, because there was nothing to log.

The confirmation moved into the page (`backglass/web/static/confirm.js`): first press
arms the button, second sends it. `hx-confirm` stays in the markup as the declaration
that the action is destructive. No timeout — the first draft disarmed after four
seconds and the arm expired between two presses while being driven, which rebuilds the
same silent no-op by hand.

Verified two ways: in WebKit, one press wrote nothing and the second sent
`POST /commitments/343/drop`; then the same two presses in the desktop app itself,
driven through `d` `d` on the keyboard — first press zero drop requests and the row
still open, second press `POST /commitments/344/drop` 200 and the row dropped.

**This changes how Drop is used.** It is two presses now: `Drop` → `Sure?` → gone, by
mouse or by `d` `d`. That is the confirmation docs/06 asks for, in the page instead of
in a dialog that this webview never had.

## Still open

1. **Quick-add moves an explicit past date a year forward.** Typing `2026-07-30` into
   the form's date field stored `2027-07-30`, silently — `dates.resolve_due` rolls a
   past date into the future, which is right for "friday" and wrong for a date the owner
   picked out of a calendar widget. Not investigated further; found while building a
   probe row.
2. Worth proposing, not built: the shell could re-navigate the window when it regains
   focus after a long idle. A dashboard open for 23 hours is showing yesterday's board,
   and nothing on either side says so.
3. A dev `uvicorn` on 8771 has been running since Friday off older code. Left alone: a
   second session is active in this checkout (it changed `config.py` and
   `extract/client.py` mid-session, an `anthropic_base_url` feature in progress), and
   that process may be theirs.
4. `ruff` (19) and `mypy` (4) report pre-existing failures, untouched by this change.
   `backglass/web/panels.py:808` assigns a `list[Chat]` to an `int` — worth a look.
