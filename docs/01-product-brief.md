# Product Brief

## The problem, stated precisely

Commitments are made in prose, scattered across inboxes, and tracked nowhere. "I'll have
the migration plan to you by Friday" is a real obligation with a real date, and it exists
only as a sentence in the middle of a thread that will be forty messages deep by
Thursday.

The obvious response is to put everything in one searchable place. That response is wrong,
and understanding why is the whole design.

A searchable archive of your own email is worse than Gmail, which already has better
search than you will build and already contains all of it. Consolidating does not create
value. What creates value is **extraction**: turning prose into typed records with dates,
counterparties, and status.

## The reframe

The system is a **commitment ledger**. Documents are evidence, not content.

This inverts the build order. The instinct is to ingest everything first and figure out
the useful part later. The correct order is to get one high-value extraction working on
one source, confirm it produces something you actually read, and only then widen intake.

It also removes an entire category of infrastructure. Because the questions are known
ahead of time — what do I owe, to whom, by when, and what changed — the expensive work
happens once at ingest and the read path is plain SQL over a small table.

## Who it is for

One person. K, a founder splitting time between Arizona and India, working across several
client engagements and a couple of products. High email volume, high commitment density,
frequent timezone changes.

Designing for one known user is a feature. It permits decisions a multi-tenant product
could not make: SQLite, no auth, no onboarding, a hardcoded working window that can be
edited in a config file.

## Success

The system works if, six weeks in:

- The brief is opened on most mornings without prompting.
- At least one thing surfaces that would otherwise have been missed. One is enough to
  pay for the build.
- Nothing in the brief has ever been confidently wrong.

The system has failed if the brief goes unread for a week, regardless of how well the
pipeline runs.

That failure mode is the reason phase 0 exists and involves no code.

## Non-goals

- Multi-user, OAuth flows, hosted infrastructure, billing.
- Semantic search, RAG, chat over the corpus.
- Writing to email or taking actions on the owner's behalf.
- Meeting transcription.
- Mobile apps. The brief is email; the dashboard is responsive web.

## The product path

Built for one, structured so a second user is a migration rather than a rewrite. Four
decisions cost almost nothing now and are expensive later:

1. `user_id` on every table, always 1.
2. Credentials in a table, not environment variables.
3. Extraction prompts versioned and stored, not inline in code.
4. The ledger schema stays source-agnostic. A commitment extracted from Slack in 2027
   fits the same row shape as one from Gmail today.

Explicitly not yet: multi-tenancy, row-level security, billing, admin panel, hosted
deployment. None of that teaches whether the product is good.
