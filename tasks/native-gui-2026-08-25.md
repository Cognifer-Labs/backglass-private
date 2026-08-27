# The GUI the recent features never got

Written 2026-08-25. The ledger grew five migrations and a dozen modules since the
dashboard was last thought about as a whole. This asks one question of each of them —
**can the owner see it, and does it feel like an app** — and it answers from the code and
the live ledger rather than from memory.

Ground truth read first (`uv run backglass state`): schema 34 applied, installed app
matches the checkout, 10,897 source items, 411 open commitments, every job loaded.

---

## What "native" means here, precisely

Two layers, and they fail differently.

**The surface** is server-rendered HTML — FastAPI, Jinja2, HTMX, plain CSS custom
properties, per docs/10. "Native" for this layer means the app behaves like one program:
every feature reachable from the same nav, the same keys everywhere, the same tile
grammar. Its failure mode is *drift* — a page ships, the nav does not learn about it.

**The shell** is `desktop/src-tauri`, 117 lines of Rust: a WKWebView over
`127.0.0.1:8765`, a dock icon, and a child process it kills on exit. `Cargo.toml` reads
`tauri = { version = "2", features = [] }` — **no plugins, no menu, no tray, no IPC.**
"Native" for this layer means the macOS things a window is expected to do. Its failure
mode is *absence* — nothing is wrong, there is simply nothing there.

---

## The survey — every recent feature, and whether it has a surface

| Feature | Module / migration | Surface today | Verdict |
|---|---|---|---|
| Coursework and classes | `coursework.py`, `courses.py` · 0031 | `/classes`, full page, in the nav | **done** |
| Situation document | `situation.py` · 0033 | panel in `/memory`, 22 versions with diffs | **done** |
| Priority tiers | `plan/priority.py` | panel in `/schedule` | **done** |
| Motion | `web/static/motion.js` | loaded from `base.html` on every page | **done** |
| Engagement distinct | 0034 | dismiss button, `_board.html:116` | **done** |
| Vault export | `vault.py` | one `obsidian://` link per lane in `/memory` | partial — 161 notes, no index or last-export state in-app |
| Scrub | `scrub.py` | `/scrub` exists; a dashboard tile appears only above `SCRUB_ALERT_FLOOR` | **unreachable** — not in the nav |
| Semantic search | `search.py` · 0021/0022 | `/ask` exists, 1,630 documents indexed | **unreachable** — linked from nowhere at all |
| Logic checker | `logic.py` | writes `notify` and `claim_event`, both invisible | **none of its own** |
| Repair loop | `repair.py` | runs on dashboard open, reported to stderr only | **fixed — A5** |
| Retraction | `retraction.py` · 0030 | — | **none** — 18 rows |
| Claim events | `claim_events.py` · 0032 | — | **none** — 421 rows |
| Notifications | `notify.py` | osascript banner, no in-app history | **none** — 28 rows |

Three of those "none" rows are the same shape and belong together; see Lane A.

---

## Findings, ranked by what the owner would actually notice

### 1. Two finished pages are unreachable

`/ask` and `/scrub` have routers, templates and tests. Neither is in the sidebar.
`grep -rn 'href="/ask' backglass/web/templates/` returns **nothing** — semantic search,
the feature the 2026-08-10 ruling was written to permit, with 1,630 documents indexed, can
only be reached by typing the URL. `/scrub` is one step better: `panels.py:872` puts a tile
on the dashboard, but only when disposable items exceed the alert floor, so the page is
invisible on exactly the days it is working.

These two are the whole list, not a sample. `/schedule/week` is the only other route
outside the sidebar and it is reachable — `schedule.html:86` links to it, and the week page
links onward to itself. Every remaining GET route is a nav entry or a detail page reached
from one.

### 2. The keyboard map and the sidebar disagree

`base.html:102` says *"Page switching mirrors the sidebar order."* It does not:

```
sidebar:  /  /schedule  /classes  /goals  /people  /roadmaps  /memory  /chats  /brief  /decisions
keys:     1  2          —         3       4        5          6        7       8       9
```

`/classes` was added to the nav and never to the map, so **every key from 3 on points one
row above its label**, and the three newest pages have no key at all. The comment is the
tell: it was true when written.

### 3. The system speaks as somebody else — measured 2026-08-25, and smaller than it looked

`notify.py:262` delivers through `osascript -e 'display notification'`, and macOS files
the banner under **Script Editor**. Notification Center's own database
(`~/Library/Group Containers/group.com.apple.usernoted/db2/db`) confirms it: **100 records
under `com.apple.scripteditor2`**, decoding to Backglass titles and bodies.

**This entry originally said more than the evidence supports, and the check that corrected
it is the reason to keep the numbers here.** The suspicion was that the banners were being
suppressed — filed under an app the owner never granted notification permission to, with
`osascript` exiting 0 either way, so the ledger would be asserting a delivery that never
happened. Two controls killed that:

- **73 of those 100 records have `presented = 1`** — the highest ratio of any app on this
  machine. Mail: 0 of 3. Calendar: 0 of 2. Superset: 0 of 25. The banners are not merely
  arriving, they are the ones that most reliably show.
- **Script Editor has no entry in `com.apple.ncprefs.plist`** — but neither do Calendar or
  Superset, and 70 apps do. Absence there means "never explicitly configured", not
  "denied".

So delivery works and the ledger is telling the truth. What is actually wrong, in order of
what it costs:

1. **No click-through.** `osascript` exits immediately and Script Editor is not running, so
   nothing is there to receive a click. A banner reading "46 question(s) waiting" cannot
   take the owner to `/ask`. This is the cost paid daily.
2. **No tunable identity.** Notification style, grouping and Focus allow-lists key on the
   app. Backglass shares one row with every other script on the machine, under a name that
   is not the product. Latent today — both configured Focus modes ("Reduce Interruptions",
   "Do Not Disturb") have empty allow-lists — but it is what bites the day the owner wants
   deadlines through a Focus and finds no Backglass to allow.
3. **Cosmetic.** Wrong name, wrong icon.

That is a real but modest bill, and it should be weighed against B3's cost rather than
assumed to justify it.

### 4. 421 events of "what changed and why" have no reader

`claim_event` is the audit trail rule 1 implies, and it is genuinely broad — the writers
are the pipeline, not just the web layer:

| cause | rows |
|---|---|
| `relevance_rejudged` | 366 |
| `dropped` | 16 |
| `dependency_broken:fact:67` | 16 |
| `resolved` | 8 |
| `fact_superseded` | 8 |
| `upstream_due_moved` | 7 |

`grep` finds no reader anywhere under `backglass/web/`. Beside it, `source_item_retraction`
holds 18 rows — the calendar dropping LIA 101, a BIO 181 lecture moving off Thursday — and
those are events the owner is *specifically* meant to see, since the whole reason 0030
exists is that a vanished upstream row used to be silent.

### 5. A working button nobody has ever pressed

`schedule.py:631` accepts the day's plan, and its docstring is explicit: *"this needs a
button: the boundary is meaningless if accepting requires a terminal."* The button is
rendered at `schedule.html:17`. **0 of 88 day plans have `accepted_at` set.**

`tasks/product-gaps-2026-08-20.md` §1 left this open — "whether that is a UI problem or an
intent problem is an open question". It is at least partly a UI problem: the plan is *read*
on `/` (the Today panel) and can only be *accepted* on `/schedule`. The affordance is one
page away from the attention.

### 6. The shell does nothing a window is expected to do

No window-size persistence. No dock badge. No `backglass://` scheme, so nothing can
deep-link back into a page.

**The menu is fine, and the first draft of this document was wrong about it.** It claimed
that a Tauri 2 window with no menu has no Edit menu and therefore no working clipboard, and
called that the worst native defect here. Reading the vendored crate settles it:
`tauri-2.11.5/src/app.rs:2244` runs `if self.menu.is_none() && self.enable_macos_default_menu`
under `#[cfg(target_os = "macos")]` with no feature gate, `enable_macos_default_menu`
defaults to `true` (`app.rs:1620`), and `menu/menu.rs:215` builds an Edit submenu with
Undo, Redo, Cut, **Copy**, **Paste** and Select All — plus the app menu, File, View and
Window. `Cargo.toml`'s `features = []` does not disable it; `pub mod menu` is unconditional.

So ⌘C works, the shell already has a real macOS menu, and the shell is in better shape than
the surface is.

---

## Lane A — the surface

**A1. One Activity page.** `/activity`, unifying the three invisible tables into one
reverse-chronological trail: claim events, retractions, notifications sent. This is the
`classes.py` pattern exactly — a router, a template, a reader in a module that already
exists, **no migration** — and it is the strongest item here because it turns rule 1's
audit trail from a thing the schema promises into a thing the owner can read.

Group by day; each row states subject, what changed, old → new, and the cause. Retractions
get their own filter, because "the calendar stopped listing your Thursday lecture" is a
different question from "relevance re-judged 366 items".

**A2. Nav and keys, corrected together.** Add `/ask`, `/scrub` and `/activity` to the
sidebar; rebuild the key map from the same list. The durable fix is to stop writing the
list twice — build the sidebar and the key map from one array in `base.html` so the next
page cannot drift. That is the actual defect; the missing `/classes` key is only its
symptom.

**A3. Accept the plan where the plan is read.** Put the accept control on the dashboard's
Today panel, next to the plan it accepts. Same POST, same boundary.

**A4. Vault state on `/memory`.** Last export time and note count, from the same fields
`backglass state` already derives. Small, and it closes the loop on a feature that
currently writes 161 files and says nothing in-app about having done so.

## Lane B — the shell

**B1. Nothing — the menu is already there.** Kept as a numbered item so the finding is not
re-discovered: the default macOS menu, clipboard included, ships automatically. Do not
"add" it.

**B2. Window state.** Remember size and position across launches. With B1 gone this is the
whole of the cheap shell work.

**B2b. A Backglass item in the menu that already exists.** Since the menu is real, the
cheapest genuine native gain is putting the product's own commands in it — Sync now,
Today, Activity — each one a menu item that navigates the webview. That is additive to a
menu Tauri built, not a menu to build.

**B3. Notification identity — a fork the owner has to settle.** The discriminating
question is *must a banner fire when the app is closed?* Today it must: `notify.py` is
called from the launchd sync, which runs every 30 minutes whether or not anything is open.

- **(a) The app owns delivery.** Backglass becomes a resident (login item or tray), posts
  its own banners under its own icon, and a click deep-links to the relevant page.
  osascript stays as the fallback for when it is not running. Correct identity, real
  click-through — but it changes *how the app runs*, not just how it looks, and that is an
  owner decision rather than a polish task.
- **(b) Keep osascript.** Zero work, keeps the Script Editor attribution and the dead
  click.

Do not treat (a) as obviously right. A resident app is a different product than one the
owner opens — and on the 2026-08-25 measurements above, (b) is defensible: delivery works,
the ledger is honest, and what (a) buys is click-through and a settings row of its own.

**One unexplained observation, recorded so it is not lost and not used as evidence.** Six
Script Editor records at 20:16 and 20:19 UTC decode to `A deadline moved / Submit the Team
Charter is now due 2026-09-04 (was 2026-08-31)`. No `notification` row in the ledger
carries that body, no `commitment` matches "the Team Charter", the ledger's four
notifications that day were at 19:31 and 19:58, and the stale
`~/Library/Application Support/Backglass/data/backglass.db` has no `notification` table at
all — so it is not a second-ledger bug. Something on this machine fires osascript banners
with Backglass-shaped text that Backglass did not write. Worth pulling on; it is not an
argument for or against B3.

**B4. Dock badge** with the overdue count. Depends on B3(a) — a badge needs something
running to set it.

---

## Built, 2026-08-25

Everything in Lane A and the surviving half of Lane B. **B3 is not built** — the
resident-app fork is an owner decision about how the app runs, not a polish task, and it
has not been made.

| Item | What shipped |
|---|---|
| **A1** | `/activity` — `backglass/activity.py` (reader, no table), `web/routes/activity.py`, `activity.html`. Merges `claim_event`, `source_item_retraction` and `notification` into one time-ordered feed with stream and window filters. Live: 124 events, 76 changes, 19 retractions, 29 notices. |
| **A2** | `base.html` emits the nav **and** the digit-key map from one `pages` list. `/ask`, `/scrub` and `/activity` are in the nav; the tail three carry a group rule. `/scrub` gained a count badge. |
| **A3** | Accept-plan on the dashboard's Today panel. `dashboard_today.sql` already selected `plan_status`; nothing had read it. Same POST, same boundary as `/schedule`. |
| **A4** | `vault.status()` and a vault line on `/memory`: note count, export time, and whether the folder is also an ingest source. |
| **B2** | `tauri-plugin-window-state` — size and position across launches. |
| **B2b** | A **Go** menu: Dashboard ⌘1, Schedule ⌘2, Activity ⌘0, Ask ⌘K, Scrub ⌘J. It hands out the accelerators the page cannot: ten digits, thirteen pages, and letters belong to the dashboard. |
| **B1** | Nothing, correctly. See the correction above. |
| **A5** | The repair loop's on-open report reaches a surface. It wrote no `run` row by design and printed to stderr, which the desktop shell does not have — so a failing repair degraded and was logged but never surfaced. `repair.last()` holds the current process's pass; a pass carrying errors raises a vermilion sidebar alert naming the step. |

### Verified

- **2,630 tests pass** on the final tree. 17 net new test functions across
  `test_activity.py` (20 in a new file), `test_web_pages.py`, `test_vault.py` and
  `test_edges.py`, whose three-test keyboard class was rewritten to read the generator
  rather than a literal table. Counted from the diff; no clean before-number was ever
  observed, because both earlier full runs stopped at their first failure.
- The shell was launched and driven: `System Events` reports the menu bar as
  `Apple, Backglass, File, Edit, View, Window, Help, Go`, the Edit submenu as
  `Undo, Redo, Cut, Copy, Paste, Select All` — the empirical end of the B1 question — and
  clicking **Go ▸ Activity** navigated the webview, confirmed by screenshot.
- The page was read on the owner's real ledger, not a fixture — and end to end on the
  artifact the owner opens: with port 8765 free, launching `/Applications/Backglass.app`
  spawned its own frozen sidecar
  (`Backglass.app/Contents/Resources/sidecar/backglass-server dashboard`, confirmed by
  `lsof`) and served `/activity`. `backglass state` reports `matches_source: True` over 44
  frozen surfaces and the Python manifest.
- **Dark mode was audited, not observed.** The in-app toggle could not be driven from
  `System Events`, and Safari returned a stale screenshot (the known sticky-screenshot
  artifact). What is checkable was checked: the page's CSS contains **no literal colour**
  — 11 tokens, all defined in `design/tokens.css`, and all six colour tokens
  (`--ink`, `--ink-muted`, `--on-ink`, `--rule`, `--rule-hair`, `--verm-line`) carry a
  dark-mode redefinition. Worth a human glance at the toggle regardless.
- `ruff` clean on every file touched; `mypy` gained no new error (the nine in `vault.py`
  and three in `panels.py` are pre-existing and untouched).

### Two things found while building

**A test was already failing on the calendar, not on this work.**
`tests/test_questions.py::TestTheAskPage` pinned `MONDAY = date(2026, 8, 24)` and reads
the real wall clock through the page; `_conflicts` looks forward `HORIZON_DAYS` from today
and never back, so the fixture's classes fell out of the window at midnight. Green when
written, red the next morning, nothing in the diff to explain it — the 2026-08-24 lesson
one scale up. Fixed by pinning the app's clock in the fixture rather than moving the
constant forward, which would only put it back the following week.

**The spacing was wrong on the first pass, and the reason is worth keeping.** `.row` is
already a tile under the 2026-08-07 spacing pass — 16px inside, 12px between, 4px between
lines within one. The first draft of the Activity CSS set its own margins on top of that,
which put 40px between a day heading and the tiles it heads while the tiles sat 12px
apart, so the heading read as floating between two groups instead of belonging to the one
below it. The second pass measures everything against that scale and adds no new step.

## Still open

- **B3, the notification identity.** `notify.py` still delivers through `osascript` and
  the banner still says Script Editor. The fork in this document is unchanged and
  unanswered: a resident app that owns delivery, or the current attribution.
- **B4, the dock badge.** Depends on B3(a) — a badge needs something running to set it.

## Constraints that bind all of the above

- **`design/design-system.md` governs every pixel.** No shadows, no gradients, radii from
  the §7 scale with nested radii concentric, every coloured fill carries a black keyline,
  three chart series maximum, tabular figures. `design/preview.html` is the reference to
  match. A new page that invents its own tile grammar is a defect, not a variation.
- **Panel markup needs `id="panel-…"` anchors.** `tests/conftest.py::panel_slice` bounds
  fragments on them; CLAUDE.md forbids `.split()` on closing tags. Any new panel ships with
  its anchor and a test in `test_web_pages.py`.
- **Every template or CSS edit makes the installed app stale.** The sidecar freezes
  templates and CSS at build time, and `backglass state` currently reports
  `matches_source: True`. It will report a stale surface the moment Lane A starts; the work
  is not shipped until the sidecar is rebuilt.
- **A test touching `local_now()` must pin its window** — the 2026-08-24 lesson. Anything
  asserting a notification was recorded needs
  `settings.model_copy(update={"notify_window": "00:00-23:59"})` or it silently reports the
  hour the suite ran at.
- **Empty states are declarative**, per docs/06. "Nothing recorded." Never "You're all
  caught up! 🎉".

## Order

The B1 probe was run while writing this and removed its own item, which moves the surface
work to the front — where the actual defects are.

1. **A2** — nav and keys from one list. Smallest change, and it makes two finished features
   reachable, which is the cheapest real gain available.
2. **A1** — the Activity page. The largest and the most valuable.
3. **A3, A4** — small, once A1's patterns exist.
4. **B2, B2b** — shell polish, after the surface is right.
5. **B3** — only after the owner settles the resident-app fork.

## What is verified, and what is not

Verified by reading the code and querying the ledger: the route list and the sidebar list
and their disagreement; that `/ask` is linked from nowhere; the keyboard map's contents;
`osascript` as the delivery path; the row counts (421 claim events across six causes, 18
retractions, 28 notifications across five kinds, 88 day plans with zero accepted); that the
accept route and its button both exist; that `Cargo.toml` declares no Tauri plugins and
`main.rs` installs no menu.

Verified against the vendored crate after the first draft asserted the opposite: Tauri 2
installs the macOS default menu automatically and it carries a working Edit submenu, so the
clipboard is not broken and B1 is not work.

Not verified, and stated as unknown rather than assumed: whether the plan is unaccepted
because the button is misplaced or because the owner does not want to accept plans; the
exact Tauri 2 plugin names for notifications, deep links and window state, which must be
read from the current docs rather than cited from memory — the B1 correction is precisely
what citing an API from memory costs.
