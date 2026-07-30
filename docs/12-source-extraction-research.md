# Source extraction research

Survey of open-source projects that parse our sources better than the naive approach, and
which of their techniques Backglass should adopt. Research date: 2026-07-30. Rule applied
throughout: prefer vendoring a technique over adding a dependency; a dep gets added only
when the format is genuinely hard (binary, evolving, reverse-engineered).

## 1. iMessage (chat.db)

**Reference projects.** [imessage-exporter](https://github.com/ReagentX/imessage-exporter)
(Rust, ~5.5k stars, actively maintained through macOS Tahoe 26.x, GPL-3.0) is the gold
standard. Its author documented the `typedstream` format in
[Reverse Engineering Apple's typedstream Format](https://chrissardegna.com/blog/reverse-engineering-apples-typedstream-format/).
Python peers: [python-typedstream / `pytypedstream`](https://github.com/dgelessus/python-typedstream)
(pure Python, LGPL-3.0, 32 stars, quiet but the format is frozen since NeXTSTEP so churn
risk is near zero) and [imessage_tools](https://github.com/my-other-github-account/imessage_tools)
(heuristic parser, scans the blob for the `NSString` marker + length prefix).

**Techniques worth taking.**

- **Null-text recovery.** Since Ventura, many rows have `text IS NULL` and the real body
  lives in the `attributedBody` BLOB — a typedstream-serialized `NSMutableAttributedString`.
  A naive `SELECT text` silently drops those messages. Decode order: try a real typedstream
  parse; fall back to the `NSString`-marker scan for malformed blobs.
- **Tapback filtering.** Reactions are separate message rows: `associated_message_type`
  2000–2005 = tapback added, 3000–3005 = removed. Filter `associated_message_type = 0`
  for real messages, or a heart lands in the ledger as a message
  ([reference](https://grokipedia.com/page/iMessage_chatdb)).
- **Edited messages.** Post-Ventura edits live in the `message_summary_info` BLOB (also
  typedstream); imessage-exporter extracts per-edit content and timestamps. For Backglass,
  the latest text is enough — but detect the edited flag (`date_edited != 0`) so a re-run
  re-extracts, which interacts with idempotency hashing. Unsent messages have no content.
- **Group naming.** Use `chat.display_name`; when empty (most groups), fall back to a
  sorted join of participant handles so the same group hashes stably across runs.

**Adopt:** add `pytypedstream` (pure Python, no transitive deps — LGPL is fine as an
unmodified library dep) with the vendored NSString-scan fallback (~30 lines). Filter
tapbacks, flag edits. **Effort: S–M.**

## 2. PDF text (drop folder + Drive)

Comparison for text-first extraction at personal scale
([py-pdf benchmarks](https://github.com/py-pdf/benchmarks),
[speed-vs-license writeup](https://pdfmux.com/blog/pymupdf-vs-pdfplumber/)):

| library | license | weight | verdict |
|---|---|---|---|
| [pypdf](https://github.com/py-pdf/pypdf) | BSD-3 | pure Python, zero deps | adequate text quality, very active |
| [pdfminer.six](https://github.com/pdfminer/pdfminer.six) | MIT | pure Python, slower | better layout analysis, sleepier maintenance |
| [pdfplumber](https://github.com/jsvine/pdfplumber) | MIT | wraps pdfminer.six | best tables/debugging; overkill for text-first |
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) | **AGPL-3.0** | ~50 MB compiled wheel | fastest, best quality — license + weight both fail our bar |

**Adopt:** add **pypdf**, nothing else. Pure Python, permissive, one dep, and extraction
quality is fine for "a model reads it once" — we need words, not layout. Escalate to
pdfplumber only if table-heavy documents start mattering. Guard: if extracted chars/page
falls below a threshold, mark the item `needs_ocr` and skip — do not add OCR now.
Future-only: [ocrmypdf](https://github.com/ocrmypdf/OCRmyPDF) (MPL-2.0) is the right
escalation but drags in tesseract + ghostscript system deps. **Effort: S.**

## 3. Gmail — quoted-history and signature stripping

**State of the field is bad.** [mailgun/talon](https://github.com/mailgun/talon)
(1.3k stars, Apache-2.0) — last real commit **Feb 2022**; deps include lxml, regex, and
scikit-learn for the ML signature mode. [zapier/email-reply-parser](https://github.com/zapier/email-reply-parser)
(~518 stars) — last commit **2020**. [mail-parser](https://github.com/SpamScope/mail-parser)
solves a different problem (MIME structure), which stdlib `email` already covers.
Nothing better-maintained has emerged; everyone with money moved to ML services.

**Adopt: vendor, do not depend.** Take talon's plain-text quotation patterns
(`talon/quotations.py`: the "On DATE, NAME wrote:", "-----Original Message-----",
"From:"-block, and `>`-prefix line-classification patterns) plus email-reply-parser's
`SIG_REGEX`/`QUOTE_HDR_REGEX`, as one vendored module with attribution (Apache-2.0 and MIT
both permit this). Skip talon's HTML and ML paths entirely — extract `text/plain` parts
and strip there. Rationale: both projects are abandonware, so a dep buys ongoing risk and
zero maintenance; the regexes themselves are stable artifacts. Stripping is a token-cost
and dedup win (thread hashing, per `docs/07-connectors.md`), and the extractor model
tolerates any residue. Build the fixture set from K's real thread shapes (Gmail, Outlook,
Apple Mail reply styles). **Effort: M** (the fixtures are the work, not the regexes).

## 4. Slack — threads, cursors, rate limits

**Reference:** [slackdump](https://github.com/rusq/slackdump) (Go, 2.7k stars, active).

- **Confirmed: `conversations.history` does not return thread replies.** Only parent
  messages appear (plus replies explicitly broadcast to the channel, subtype
  `thread_broadcast`). A history-only reader silently loses every threaded answer —
  including "yes, I'll get that to you Friday". Pattern: in the history page, any message
  with `reply_count > 0` (or `thread_ts == ts`) is a thread parent; call
  [`conversations.replies`](https://docs.slack.dev/reference/methods/conversations.replies/)
  per parent, cursor-paginated via `response_metadata.next_cursor`, `limit <= 200`.
  Dedup note: the parent message is returned again as the first item of `replies`.
- **Rate limits changed May 2025:** non-Marketplace apps created after 2025-05-29 get
  `conversations.history`/`replies` at **1 request/minute, max 15 items** — brutal for
  backfill, workable for an incremental daily sync. Existing installs keep old tiers.
  slackdump's answer: honor `Retry-After` on every 429 with exponential backoff, never
  pre-compute pace. Budget the first backfill in days, then cursor forward.
- **Cursor:** persist `oldest`/`latest` timestamps per channel plus per-thread
  `latest_reply` so replies to old threads are still picked up.

**Adopt:** vendor the pattern (parents from history → replies fan-out → Retry-After
backoff) in our stdlib-HTTP connector. No dep. **Effort: M.**

## 5. GitHub — obligations feed

`search/issues?q=involves:@me` is state-based, rate-limited to 30 req/min, and eventually
consistent. The [Notifications API](https://docs.github.com/en/rest/activity/notifications)
(`GET /notifications`) is a better commitments feed:

- Each notification carries a **`reason`** — `review_requested`, `mention`, `assign`,
  `author`, `team_mention`, `subscribed`, `state_change`, `ci_activity` — which maps
  almost one-to-one onto commitment types. `review_requested` **is** an obligation.
- **Polling is designed in:** send `If-Modified-Since` from the returned `Last-Modified`;
  a `304` costs zero rate limit; honor the `X-Poll-Interval` header. Pagination is
  page-based (`per_page`/`page` via the `Link` header), not cursors.
- Caveat: notifications are one-shot events. Once read they leave the default list (use
  `?all=true`), and they never say whether the PR is *still* awaiting you. So: notifications
  for **discovery**, then a targeted `user-review-requested:@me` search as the open-state
  check ([discussion](https://github.com/orgs/community/discussions/56926)) —
  `review-requested:@me` stays true after you review; the `user-` variant clears.

**Adopt:** vendor the pattern — notifications poll for intake, state query at brief time
to close resolved items. No dep; `gh api` in dev, stdlib HTTP in the connector.
**Effort: S.**

## 6. Calendar / Drive / Docs

- **Docs tabs (2024+).** Documents now have a tab tree; anything not tab-aware sees only
  the first tab. `documents.get` needs `includeTabsContent=true`, then a recursive walk of
  `tabs[].documentTab.body` and `childTabs`
  ([Work with tabs](https://developers.google.com/workspace/docs/api/how-tos/tabs),
  [extract-text sample](https://developers.google.com/workspace/docs/api/samples/extract-text)).
  Drive `files.export?mimeType=text/plain` is the simpler path but its multi-tab behavior
  is not documented — test on a tabbed doc; if it exports only tab one, switch to the
  Docs API walk. Assume multi-tab docs exist in K's Drive.
- **Recurring events: never expand RRULEs yourself.** Pass `singleEvents=true` with a
  bounded `timeMin`/`timeMax` ([guide](https://developers.google.com/workspace/calendar/api/guides/recurringevents)).
  Pitfalls the API handles that hand-rolling gets wrong: deleted occurrences arrive as
  `status=cancelled` instances rather than parent EXDATEs; moved instances keep
  `originalStartTime` + `recurringEventId` (use that pair as the stable instance identity);
  expansion needs the series timezone or weekly events drift across DST — which bites a
  UTC-7 ↔ UTC+5:30 owner twice over.
- **Incremental:** `syncToken` for delta sync; on `410 GONE` drop the cursor and rescan the
  bounded window (fits the connector cursor contract in `docs/07-connectors.md`).

**Adopt:** vendor both patterns. No dep. **Effort: S each.**

## 7. WhatsApp — verdict: skip

There is no path that is simultaneously local, ToS-tolerable, and Python. The live-client
routes ([whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js),
[mudslide](https://github.com/robvanderleek/mudslide), both Baileys/Node) impersonate
WhatsApp Web — explicitly against Meta's ToS, with a real account-ban risk, on a protocol
that breaks without notice (mudslide's own README says don't rely on it). The offline
route, [wa-crypt-tools](https://github.com/ElDavoo/wa-crypt-tools) (Python, decrypts
crypt14/15 backups), is technically clean but needs the Android backup key from the
phone's app-private storage — an Android-centric, manual, per-backup ritual, and K would
be feeding stale exports. Wrong risk/reward for a personal system whose other sources are
all stable. Revisit only if WhatsApp ever ships a personal-data API. **Do nothing.**

## 8. Apple Notes / Reminders — future sources

**Notes.** The store is `NoteStore.sqlite` with note bodies as **gzip-compressed protobuf**
in `ZICNOTEDATA.ZDATA` — not recoverable with regex. Reference implementations:
[apple_cloud_notes_parser](https://github.com/threeplanetssoftware/apple_cloud_notes_parser)
(Ruby, forensics-grade, tracks each OS release) and its Python port
[apple-notes-parser](https://github.com/RhetTbull/apple-notes-parser) (MIT, Python 3.11+,
16 stars but active, single runtime dep: `protobuf`). Zero-dep alternative: JXA via
`osascript` returns each note's `body` as HTML — no schema risk, but slow and loses tags.
**Adopt when the source lands:** `apple-notes-parser` as a dep (it earns its place — the
format is reverse-engineered and shifts with OS releases; let RhetTbull chase it). Same
read-only-copy discipline as chat.db. Skips password-protected notes. **Effort: M.**

**Reminders.** No public API. Two local routes: JXA/AppleScript via an `osascript`
subprocess (zero deps, but Reminders scripting is notoriously slow at hundreds of items
and JXA has known specifier gaps —
[Apple forums](https://developer.apple.com/forums/thread/649653)), or EventKit via
`pyobjc-framework-EventKit` ([API notes](https://pyobjc.readthedocs.io/en/latest/apinotes/EventKit.html))
— fast, typed, but a binary dep plus a TCC permission grant for the Python binary.
**Adopt when the source lands:** start with `osascript -l JavaScript` emitting JSON
(vendored ~40-line script, incomplete-only filter keeps it fast at personal scale);
escalate to pyobjc-EventKit only if runtime hurts. **Effort: S, M for EventKit.**

## Prioritized shortlist

1. **Slack thread replies** — without `conversations.replies` fan-out, threaded
   commitments are invisible. Vendor the parents→replies pattern + Retry-After backoff
   into the connector; persist per-thread cursors. (M)
2. **iMessage `attributedBody` decode** — post-Ventura, a large share of messages have
   NULL `text`. Add `pytypedstream` + vendored NSString-scan fallback; filter
   `associated_message_type != 0`; flag edits via `date_edited`. (S–M)
3. **GitHub notifications as the obligations feed** — `reason=review_requested` etc. is a
   typed commitment stream; poll with `If-Modified-Since`/`X-Poll-Interval`, close items
   at brief time with a `user-review-requested:@me` state check. Replaces `involves:@me`
   as primary. (S)
4. **pypdf for the drop folder + Drive PDFs** — single BSD pure-Python dep, `needs_ocr`
   threshold flag instead of an OCR stack. (S)
5. **Vendored quote-strip regex module** — talon + email-reply-parser plain-text patterns,
   vendored with attribution, fixture-tested on K's real reply styles. Both upstreams are
   dead; the regexes aren't. Feeds thread dedup and cuts extraction tokens. (M)

Deliberately not doing: PyMuPDF (AGPL), any talon/email-reply-parser dependency
(abandoned), WhatsApp (ToS + fragility), OCR (premature), Docs-tab support beyond the
documented `includeTabsContent` walk.
