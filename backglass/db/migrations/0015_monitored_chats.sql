-- Which conversations the ledger is allowed to read, as a decision rather than a setting.
--
-- The allowlist has lived in `IMESSAGE_CHATS` and `INSTAGRAM_CHATS` since each connector
-- was written, which has two problems. Choosing means hand-editing .env with a chat's
-- display name or a raw phone number spelled from memory, and getting it slightly wrong
-- fails silently — an allowlist that matches nothing looks exactly like a quiet week.
-- And a conversation the list does not name is dropped without trace, so a new group
-- chat where plans are actually being made never surfaces at all.
--
-- Both are the same mistake: treating consent as configuration. The review queue already
-- shows the better shape — the system finds something, the owner decides once, and the
-- decision is remembered — so a conversation gets a row here the first time it is seen,
-- and `decision` carries what the owner said about it.
--
-- `decision` NULL is the load-bearing state. It does not mean "off"; it means *seen and
-- not yet decided*, which is what raises the prompt. `ignore` is a real answer and is
-- stored precisely so the same chat is never asked about twice — without it, every sync
-- would re-ask about every group the owner has already declined.
--
-- Nothing is ever monitored by default. A new conversation is a question.
--
-- `key` is what the connector matches on and is not the same thing as a display name: a
-- group is keyed by its title, a one-to-one by the other party's handle (a phone number
-- or address), because that is all the Messages store has for it. `display_name` is what
-- a person should read on the page, which for a one-to-one may be the same raw handle
-- until an entity resolves it.
--
-- Keyed by (source, key) rather than by key alone: "Family" on iMessage and "Family" on
-- Instagram are different conversations, and a decision about one says nothing about the
-- other.

CREATE TABLE monitored_chat (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL DEFAULT 1,
  source        TEXT    NOT NULL,          -- imessage|instagram|instagram:live
  key           TEXT    NOT NULL,          -- title for a group, handle for a one-to-one
  display_name  TEXT,
  kind          TEXT    NOT NULL DEFAULT 'group',  -- group|dm
  decision      TEXT,                      -- monitor|ignore|NULL = not yet decided
  participants  INTEGER,                   -- best-effort, for the page's "12 people"
  messages_seen INTEGER NOT NULL DEFAULT 0,
  first_seen_at TEXT    NOT NULL,
  last_seen_at  TEXT    NOT NULL,
  decided_at    TEXT,
  UNIQUE (user_id, source, key)
);

-- The page's main question is "what have I not answered yet", so that is the index.
CREATE INDEX idx_monitored_chat_undecided ON monitored_chat(user_id, decision, last_seen_at);
