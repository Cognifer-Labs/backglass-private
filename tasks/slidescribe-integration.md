# Lectures as a source: putting Slidescribe behind the ledger

Written 2026-08-24, after reading both sides rather than proposing from either. Slidescribe
is a finished tool at `~/Downloads/slidescribe` (Python 3.12, PyMuPDF + python-pptx +
FastAPI, 107 tests, artifact `06abbe91`). It takes a raw transcript and the deck it was
spoken over and returns one document: the talk cleaned and attributed, cut into sections
at the slide boundaries, each slide sitting above the passage it belongs to. Alignment is
TF-IDF plus a monotone DP over verbal anchors — deterministic, no model in the path.

This records what integrating it costs, what is already blocked, and the one question that
has to be answered before Lane B is worth starting.

---

## Why this belongs in the ledger at all

Backglass reads what a professor **writes** — Canvas ICS, the syllabus PDFs in the drop
folder, mail. It reads nothing of what a professor **says**. Half of every deadline in a
class is spoken out loud: "the exam covers thirteen through fifteen", "the charter moved
to Thursday", "bring the lab notebook Monday". None of that reaches `commitment`, and the
five CIS236 dates that had already moved when `0031_assignment.sql` was written are the
same failure from the other direction.

Slidescribe's output is the shape rule 1 wants. A `Paragraph` carries `speaker`,
`raw_text`, `slide_index`, `start_ms`; an `Alignment` carries `score`, `reason` and
`anchor_phrase`. That is provenance at a resolution nothing else in the ledger has — a
brief line can say *CHM 113, 2026-09-04, slide 7, minute 34* and link to the sentence.

So the framing is not "add a slide tool". It is: **lectures become a source, extraction
turns spoken sentences into commitments, and the aligned document is the evidence.**

## Where it sits against the rules

| Rule | Verdict |
|---|---|
| 1 — every claim links to its source | Improved. Slide index + `start_ms` + the raw utterance is better provenance than an ICS line. |
| 3 — idempotent | At risk. Slidescribe is byte-identical across runs **only with the LLM pass off**. See the `--llm off` decision below. |
| 4 — relative dates resolve against `occurred_at` | At risk. A lecture document's `occurred_at` must be the lecture date, never the file mtime. See caveat A3. |
| 7 — spend cap enforced in code | At risk. Slidescribe's formatting pass bills the signed-in `claude` CLI and knows nothing about `costs.py`. Off by default on the ingest path. |
| Ledger primary, search additive (2026-08-10) | Clean. The aligner *finds structure in a document*; it does not *state a fact*. If alignment vanished you would get one unsectioned transcript and every surface would still be correct. |

---

## Lane A — zero code in this repo, works this week

Owner runs Slidescribe by hand and drops the export into
`~/Documents/ASU Fall 2026/<COURSE>/lectures/`. `connectors/files.py` already ingests
`.md`. Nothing here needs a migration, a connector or a schema change.

Three caveats, all verified against the code and all real:

**A1 — the extraction ceiling truncates the back half of the lecture.**
`files.py:68` — `MAX_CHARS = 20_000`. A ninety-minute lecture is roughly 12,000 spoken
words, near 70,000 characters. Ingested as one file, `body = body[:MAX_CHARS]` keeps the
first quarter and the rest never reaches extraction — silently, with no error and no
count. Every deadline stated after the twenty-minute mark is lost.

*Fix:* export **one file per section**, not one per lecture. Slidescribe already cuts
sections at slide boundaries, so this is a `--format` variant in that repo, not a redesign
here. A section is also the right extraction unit: it is one slide's worth of talk.

**A2 — re-export becomes a silent conflict.**
`files.py:257` — `external_id = relative.as_posix()`. Path-keyed. `upsert_source_item`
treats a differing `content_hash` on a stored `external_id` as a conflict: it records it
and **skips**, because 0002 makes the item immutable. `0031_assignment.sql` documents this
exact trap at length.

Slidescribe's review screen exists so a human can move a boundary and re-export. So under
a naive path-keyed drop, **every human correction produces a conflict and propagates
nothing**, which is the worst available outcome: the owner corrects the document, sees the
file change on disk, and the ledger keeps the wrong version.

*Fix:* the export filename carries a revision (`…-v2.md`), or Lane B gives lectures their
own `external_id` scheme. Do not skip this — it is not a rare edge, it is the normal
workflow.

**A3 — the date must come from the lecture, not the drop.**
`_occurred_at` (`files.py:295`) believes markdown frontmatter `date:` / `created:` / `day:`
over mtime, precisely for rule 4. So the export must write `date: <lecture date>` in its
frontmatter. Without it, "the quiz is Friday" spoken on the 4th and dropped on the 20th
resolves against the 20th.

**A4 — do not let the vault eat its own output.** If the document also lands in Obsidian,
it needs `backglass: generated` frontmatter, per the existing vault rule, or the ledger
re-ingests its own product.

Lane A therefore costs one export-format change in the Slidescribe repo and **zero lines
in backglass**.

## Lane B — a `lectures` connector

`backglass/connectors/lectures.py`, same shape as `files.py`: a folder watcher with a
cursor, no credential row, no API.

- Watches a folder of `(transcript, deck)` pairs keyed by course and date.
- Shells out: `uv run --project <configured path> slidescribe run … --llm off`.
- Ingests **per section**, `external_id = lecture:<course>:<date>:<section-index>`, which
  fixes A2 by construction — a re-solve that moves a boundary changes which section a
  paragraph belongs to, not the identity of the section.
- `occurred_at` = the lecture datetime from the folder/filename convention, not mtime.
- `raw_json` carries `slide_index`, `heading`, `start_ms`, `alignment.reason`,
  `alignment.score`, `cleaned_by` — so the review surface can distinguish a boundary set
  by a verbal anchor from one set by a similarity score, which is the whole point of
  Slidescribe recording `reason` in the first place.
- Course join is already solved: `courses.py::_subject` reduces `CHM113`, `CHM 113` and
  `2026FallC-T-CHM113-60105` to the pair `("CHM 113", section)` with a regex over a code
  the registrar issued. A folder named `CHM113-Lab` joins to the `/classes` card for free.
- The Slidescribe path lives in `Settings`. **No hardcoded `~/Downloads/slidescribe`.**

Whether this needs a table is open. `assignment` is the precedent for "a live upstream
record beside the immutable item that first reported it", and a lecture has the same
property — the document is re-solvable, the utterance is not. But `courses.py` is the
counter-precedent: a whole page with no new table. Decide after Lane A has run for a
couple of weeks and it is clear whether re-solves actually happen.

---

## Blockers and decisions

| Thing | Problem | Decision |
|---|---|---|
| **PyMuPDF** | AGPL, ~50 MB. `files.py:18` records the explicit docs/12 §2 ruling **rejecting** it in favour of pypdf. Slidescribe depends on it. | Never add Slidescribe to this project's dependency graph. Subprocess boundary only. This closes the "just import it as a library" option permanently — do not relitigate it. |
| **The LLM formatting pass** | Non-deterministic, so two runs over one input are not byte-identical → rule 3 broken. It also bills the signed-in `claude` CLI outside `costs.py`, so rule 7's cap is not enforced over it. | `--llm off` on the ingest path, always. If the prose is worth cleaning, do it once at authoring time in Slidescribe's own review screen, never at sync time. |
| **TF-IDF similarity** | It is a score, and CLAUDE.md is careful about scores. | Permitted under the 2026-08-10 boundary. It finds structure in a document; extraction still forms every belief. Nothing in the brief, planner or dashboard may read `alignment.score` as a fact. |
| **PPTX thumbnails** | Slidescribe cannot rasterise PPTX on this machine (no LibreOffice) and records `thumbnail_kind: synthetic`. | Irrelevant to the ledger — backglass wants the text, not the picture. Note it so nobody treats a synthetic card as a render. |

## The transcript probe — run 2026-08-25, and it reverses the plan

Backglass has no audio path: `grep -rE "whisper|audio"` over `backglass/` returns nothing
but a `coursework.py` regex for the word "video". The probe asked what this Mac already
writes to disk, before proposing anything new. **All four candidates are empty, and the
right answer turned out to be somewhere else entirely.**

### The four candidates, all dead

| Store | Path | Finding |
|---|---|---|
| Voice Memos | `~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/CloudRecordings.db` | 136 recordings, **no transcript column** on `ZCLOUDRECORDING`. `.CloudRecordings_SUPPORT/_EXTERNAL_DATA` is empty. `ZAUDIOFUTURE` is 64 bytes — a UUID plus a digest, not text. Newest recording 2026-03-16; nothing from this semester. macOS renders the transcript in the app and does not persist it here. |
| Notes | `~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite` | `ZICCLOUDSYNCINGOBJECT` does carry `ZNEEDSTRANSCRIPTION` and `ZTEMPORARYTRANSCRIPTDATA` — **both NULL on all 324 rows**. Zero audio attachments: 43 jpeg, 11 tables, 5 url, 3 drawing, 2 png, 1 zip, 1 heic, 1 pdf. |
| System live transcription | `~/Library/Accessibility/com.apple.RTTTranscripts.sqlite` | `ZTTYHISTORY` — 0 rows. It is accessibility RTT for phone calls, not speech capture. `CallHistoryDB` is call metadata. The Biome `*.Transcript.*` streams are Siri and App Intents. |
| Audio on disk | `~/Documents`, `~/Downloads`, `~/Desktop`, `~/Movies`, 120 days | No lecture audio at all. App sound effects, one demo `.mp4`, and scipy's test `.wav` fixtures. |

So Whisper is not the answer, and neither is any recorder: **nothing on this Mac needs to
record anything.**

### What is actually there — the courses publish the transcripts

**Already in the drop folder.** Fourteen of the twenty-eight PDFs under
`~/Documents/ASU Fall 2026` are transcripts the course published:
`BIO181-Lab/Alien Zoo transcript.pdf`, `DSL Scheduling and Expectations - Transcript.pdf`
(in both BIO181-Lab and CHM113-Lab), `Statistics Tutorial Part 1/2 script.pdf`, and
`Excel tutorial 1–10 script.pdf`. All read cleanly through `pdf_text` and all are ingested
by `connectors/files.py` **today**.

They are prose. No speaker tags in any of Slidescribe's four formats, no timestamps, and —
the part that matters — **no deck**, because they are screencasts. Slidescribe's aligner
has nothing to align them against, so on these it degrades to its cleaning pass, which is
a much smaller win and not worth an integration.

**And Canvas publishes both inputs, paired, per video assignment.** `assignment_material`
row 1 is the whole thesis in one string:

```
Download Slides | [Transcripts] (https://mediaplus.asu.edu/embedded/transcript?id=…&siteId=…)
```

A deck and its transcript, published by the course, next to each other. That is exactly
Slidescribe's input pair, already named in the ledger, with no recording step anywhere.

### Why only one of them is in the ledger

Of 199 assignments: 114 have a description at all, **8 mention "Watch the video"** — all
CIS236 — and exactly **1 carries a mediaplus transcript URL**. The `Download Slides`
anchor in that same row arrives as bare text with **no href**.

The ICS feed is a degraded copy of the Canvas page. It keeps the markdown link that had
one and drops the anchor that pointed at the deck. `coursework.py` counts 38 of 169
assignments as videos from their titles, so the feed is hiding roughly thirty pairs it
knows exist.

The transcript URL is also not fetchable headless. It answers `307` to
`api-v3.mediaplus.asu.edu/v3/media/open-graph-redirect`, then serves a 3,055-byte
JavaScript shell — client-rendered, session-gated. That is the **same wall the Canvas work
already hit**: no student API token exists at ASU, and a Safari session is the only channel
that reads these pages.

### What this changes

Lane B is **not** a folder-watcher over recordings. It is an extension of the existing
Canvas-through-Safari channel:

1. For each video assignment, read the Canvas page in Safari — not the ICS feed — and
   recover the two links the feed drops.
2. Fetch the deck and the transcript through that same session.
3. Run Slidescribe over the pair with `--llm off`.
4. Ingest per section.

Every constraint in this document survives that change: the ceiling, the `external_id`
scheme, the `occurred_at` rule, the PyMuPDF boundary, the spend cap. Only the input step
moved — from a microphone that does not exist to a browser session that already works.

**The honest cost note:** step 1 is the expensive one and it is not Slidescribe's problem.
Reading the Canvas assignment pages properly is a Canvas-connector task worth doing on its
own merits — it is the same thirty video assignments whose durations `coursework.py`
already reads out of their titles, and the same feed whose descriptions arrive truncated.
Slidescribe is the thing that becomes possible *after* that lands, not a reason to do it.

## The Canvas read — run 2026-08-25, and it unlocks more than Slidescribe

The probe said the blocker was Canvas reach. It is not, any more.

### The workaround: the Canvas REST API answers on the session cookie

From a signed-in `canvas.asu.edu` tab, `fetch('/api/v1/…', {credentials: 'same-origin'})`
returns the real API — JSON, `Link`-header pagination, the lot. **No student token is
needed and none is being issued.** `docs/07`'s warning that "some institutions disable
student-generated tokens… the fallback is the ICS feed, which loses submission state" has
a third option: the browser the owner is already logged into.

Verified end to end on 2026-08-25:

| Check | Result |
|---|---|
| `GET /api/v1/courses?enrollment_state=active&per_page=100` | 200, **14 enrolments**, 9 of them real courses |
| `GET /api/v1/courses/:id/assignments?per_page=100` | 200 on all 9. **300 assignments** against the ledger's 199 |
| Descriptions | Full HTML with **real `href`s**. The ICS feed carries a description for 114 of 199 and strips anchors |
| `GET /api/v1/courses/:id/modules?include[]=items` | 200 on all 9 — the module tree, which the ICS feed does not have at all |
| `GET /api/v1/courses/:id/files` | **403** for a student. Module items are the way in: `item.content_id` → `/files/:id` |
| Binary download | `Welcome to Bio 181.pdf` fetched whole: **3,178,275 bytes**, magic `%PDF-1.7`, byte-exact against the API's `size` |

That last row is the important one. The deck half of Slidescribe's input pair is fully
reachable — metadata and bytes — through a channel that already exists.

**This is worth more than Slidescribe.** `connectors/canvas.py` is written and unused
because ASU issues no token. The same session channel that reads a deck reads submission
state, module structure, and the ~100 assignments the ICS feed never mentions. That is a
`canvas.py` change on its own merits and it should not be filed under this document.

### But the pair does not exist yet

Every course was swept for decks and recordings. What is actually published, 2026-08-25:

| Course | Lecture decks | Recordings |
|---|---|---|
| BIO181 Lecture | `Welcome to Bio 181.pdf` (3.2 MB) | **none** |
| BIO181 Lab | `Week 3_ Scientific Reasoning Act 1.pdf` (3.3 MB) | **none** |
| PSY101 | `Chapter 1.pdf` (2.3 MB) | **none** |
| CHM113 ×3, HON171, LSB191, CIS236 | syllabi, worksheets, a periodic table, study guides — no decks | **none** |

BIO181 Lecture's module tree has a `🛝 LECTURE SLIDES` and a `📽️ LECTURE RECORDINGS`
subheader in **all twelve modules**. Every recordings subheader is empty. The slides
subheaders are filling in as the semester moves; one has a file so far.

The single CIS236 mediaplus transcript is not the counter-example it looked like. Its
`Download Slides` is **bare text with no `href` in Canvas itself** — the ICS feed was not
truncating anything. And the transcript will not render: loaded in signed-in Safari the
page returns a zero-length body containing only its `<noscript>` block, and guessed
`api-v3.mediaplus.asu.edu` endpoints fail CORS. It needs the Canvas-embedded frame
context, or a Media+ session the owner does not have.

### So the blocker moved again, and this time it is not technical

Slidescribe needs a deck **and** the talk spoken over it. Canvas has three decks and zero
recordings, four days into the semester. The instructors have made a place for recordings
and have not put anything in it.

Nothing to build here until that changes. The sensible move is a **watch, not a project**:
the module sweep above is twenty lines of session JS, so re-run it every few weeks and
start Lane B the week `📽️ LECTURE RECORDINGS` stops being empty. If it never fills, this
document closes and Slidescribe stays a good standalone tool that this ledger has no input
for.

## Surfaces it earns, once lectures are in the ledger

- `/classes` course card: "Lectures — 4 recorded, last 09-04", linking to the document.
- Brief: a commitment whose evidence is a spoken sentence, quoted, with slide and minute.
- Search: lecture passages are exactly the drop-folder case semantic search was unbanned
  for on 2026-08-10 — "what did he say about titration" has no typed record and never will.

---

## Order of work

- [x] **Probe the transcript stores.** Done 2026-08-25. All four are empty; see above.
      The inputs exist, but they are published by the courses and reachable only through
      a Canvas session, not produced on this machine.
- [x] **Read the Canvas pages through Safari.** Done 2026-08-25. The session-cookie REST
      API works, the deck download is byte-exact — and the recovery target turned out not
      to exist: `Download Slides` has no href in Canvas either.
- [ ] **Split the Canvas session channel out of this document** into a `canvas.py` task.
      It is worth doing for submission state and the ~100 assignments the ICS feed misses,
      independently of whether Slidescribe ever runs.
- [ ] **Watch, do not build.** Re-run the module sweep every few weeks. Lane B starts the
      week `📽️ LECTURE RECORDINGS` stops being empty in BIO181, and not before.
- [ ] **Slidescribe: per-section export**, with `date:` frontmatter per lecture and a
      revision-carrying filename. Fixes A1, A2 and A3 in the repo that owns the exporter.
- [ ] **Lane A trial**: run two real lectures by hand into the drop folder. Confirm the
      sections ingest whole, confirm `occurred_at` is the lecture date, confirm a
      re-export does not produce a conflict line in the sync report.
- [ ] **Then decide on Lane B**, with two weeks of evidence about whether re-solves
      happen, whether extraction finds real commitments in spoken text, and how big the
      transcription step turned out to be.

## Honesty about what is verified here

Verified by reading the code: the `MAX_CHARS` ceiling, the path-keyed `external_id` and
the conflict-skip behaviour, the frontmatter date precedence, the pypdf-over-PyMuPDF
ruling, the registrar-code join in `courses.py`, the absence of any audio path,
Slidescribe's dependency list and its `Paragraph`/`Alignment`/`Section` shapes.

Verified by the 2026-08-25 probe: all four Apple transcript stores are empty, no lecture
audio exists on disk, fourteen course-published transcript PDFs are already ingested from
the drop folder, one `assignment_material` row carries a real deck-plus-transcript pair,
8 of 199 assignments mention a video and only 1 carries the transcript URL, and the
mediaplus endpoint is a session-gated JavaScript shell (`307` then 3,055 bytes).

Verified by the 2026-08-25 Canvas read: the session-cookie REST API returns 14 enrolments
and 300 assignments with full HTML descriptions; module trees are readable and the files
index is 403; a 3.2 MB deck downloads byte-exact; three lecture decks exist across nine
courses and **zero recordings**; `Download Slides` has no href in Canvas either; the
mediaplus transcript renders an empty body in a signed-in browser.

Not verified, and stated as unknown rather than assumed: whether the instructors will
publish recordings at all this semester; whether an ASU recording, once published, exposes
a transcript in a readable form; whether extraction over spoken prose finds commitments at
a useful rate; whether a lecture needs a table of its own.
