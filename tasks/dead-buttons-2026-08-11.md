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

## Still open

1. **The desktop symptom is not explained by the above.** A 500 or a 503 both reach the
   failed-write strip, and the owner saw nothing at all — which means the request never
   left the page. The window had been open since Aug 10 17:58; a WKWebView whose
   WebContent process is gone renders its last frame and runs no JS, which looks exactly
   like this. Discriminator: does the Theme button in the masthead also do nothing? If
   so the page is dead, not the server, and the fix is a reload.
2. **The fix does not reach the installed app until the sidecar is rebuilt**
   (`desktop/build-sidecar.sh`). The bundled Python is frozen at the Aug 10 17:57 build.
3. **Port 8765 is currently served by a logged `uvicorn` from the checkout**, started to
   catch the click. Kill it and relaunch `Backglass.app` to go back to the sidecar.
4. A dev `uvicorn` on 8771 has been running since Friday off older code.
5. `ruff` (19) and `mypy` (4) report pre-existing failures, untouched by this change.
   `backglass/web/panels.py:808` assigns a `list[Chat]` to an `int` — worth a look.
