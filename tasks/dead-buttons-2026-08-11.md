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

## Shipped

`desktop/build-sidecar.sh` rebuilt the frozen backend and the bundle, and it was
installed over `/Applications/Backglass.app`. Verified behaviourally rather than by a
version string: the rebuilt binary, run against a copy of the real ledger with a second
connection holding the write lock, answered 200 after 6.9s and moved the row —
the same click the old binary lost. `backglass state` now reports `matches_source: True`
with no stale surfaces.

## Still open

1. **The desktop symptom is still not explained by the fix above**, and probably never
   was. A 500 and a 503 both reach the failed-write strip; the owner saw nothing at all,
   which means the request never left the page. The window had been open since Aug 10
   17:58 and its WebContent process was sitting at 10MB RSS after 22h47m — a page whose
   memory the system had reclaimed renders its last frame and runs no JS. The relaunched
   app's WebContent is at 94MB, which is what a live dashboard weighs. **Unverified until
   somebody clicks Snooze in the app window and the row moves.** If it does not: the
   sidecar logs to `/dev/null`, so swap port 8765 for `uv run uvicorn --factory --host
   127.0.0.1 --port 8765 backglass.web.app:create_app` with its log on a file, and click
   again — a POST that never appears is the page, not the server.
2. Worth proposing, not built: the shell could re-navigate the window when it regains
   focus after a long idle. A dashboard open for 23 hours is showing yesterday's board
   whether or not its JS is alive, and this is the failure mode that made a page look
   fine and act dead.
3. A dev `uvicorn` on 8771 has been running since Friday off older code. Left alone: a
   second session is active in this checkout (it changed `config.py` and
   `extract/client.py` mid-session, an `anthropic_base_url` feature in progress), and
   that process may be theirs.
4. `ruff` (19) and `mypy` (4) report pre-existing failures, untouched by this change.
   `backglass/web/panels.py:808` assigns a `list[Chat]` to an `int` — worth a look.
