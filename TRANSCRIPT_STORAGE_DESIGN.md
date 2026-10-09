# Per-message transcript storage

Chat transcripts move from one whole-transcript JSON value per chat
(`chats.messages`) to one `chat_messages` row per item, keyed by
`(chat_id, seq)`. The change ships over two releases (expand/contract), so
that at every committed state the previous release can be rolled back to and
sees every chat completely. No updater, deployment controller or database
"floor" takes part.

## Release 1 (this release)

### Rows are the authority; `chats.messages` is their mirror

- `chat_messages` holds each item's exact JSON text (`body`, the last column)
  and small lookup projections computed by `transcript_rows.attributes`:
  `message_key` (`str(id)`), the exact `message_id`, `client_id`
  (`chat_writer.cid_of`), `role`, the exact `ts` and `flags` (`EDIT_PREVIEW`,
  `HIDDEN`, `GOAL_COMPLETION`, `PROSE`, `DERIVED_CID`, `ATTACHMENTS`). Rows are dense from 0.
- Every row mutation goes through a `chat_writer` domain command calling
  `transcript_rows`; `chat_writer.create_chat` is the only way to create a chat.
  Mutations mark the chat dirty in its Session. One `before_commit` listener
  (`transcript_rows._mirror_changed_transcripts`) then, per changed chat,
  reads the bodies in primary-key order and rewrites `chats.messages` as
  `'[' + ', '.join(bodies) + ']'`, with `has_messages` and `updated_at`.
  Bodies are default (ASCII) `json.dumps` text, so this is byte-for-byte
  `json.dumps(list)`: exactly what the previous release writes and decodes
  itself. No caller can skip it. (SQLite's ordered `group_concat` builds a
  temporary B-tree per commit, about 3x slower on a 20 MB chat; the primary
  key already yields position order.)
- Cost: each committing transaction rewrites a changed chat's whole legacy
  value, the previous release's own write cost. `SQLITE_MAX_LENGTH` bounds
  one chat's value, as it always did.

### Detecting the previous release's writes without scanning

`chat_transcript_state` (one primary-key row per chat) marks a chat whose rows
are authoritative. Trigger `chats_messages_written` deletes the marker whenever
any writer updates `chats.messages`; the commit hook re-inserts it right after
this release's own update. So:

| Previous release does | Effect |
|---|---|
| updates `messages` (its ORM) | marker deleted; the chat reads from `messages` until converted again |
| inserts a chat | no marker; the chat reads from `messages` until converted |
| deletes or purges a chat | `chats_deleted` removes rows, search entries, damage copies, marker and its own search documents |
| renames a chat | title trigger updates the search entry |

`chats` itself is unchanged: no column, rename or rebuild. Every trigger names
only columns the previous release maps, and its startup checks (`create_all`,
the migration ledger, `mapped_schema_gaps`, the readiness table probe) ignore
the additions. Its own search tables are left to it; it reconciles them from
`updated_at` after a rollback.

### Reads never convert

- While `chats.messages` exists, an unconverted chat's authority is its
  legacy value, so every `transcript_rows` reader (`history`/`History`, `at`,
  `count`, `iterate`, `reverse_iter`, `read_all`, `metadata`,
  `assistant_index`, `client_message_seq`, `attachment_bodies`,
  `max_timestamp`) reads it for such a chat, decoding it once per call or
  `History` handle, exactly as the previous release reads it. A value that
  is not a JSON list reads as the damage placeholder (a JSON `null` as an
  empty transcript), writing nothing. A converted chat reads its rows.
- So no reader converts, waits for the writer or depends on its thread,
  route or startup order: an event-loop reader that "forgot to convert" is
  not a possible bug. The marker and the value it selects come from one
  statement, or from one snapshot under `pin_read_snapshot`.
- Only a mutation converts (`transcript_rows.convert`), inline in its own
  transaction, which is the writer's: the change and the conversion commit
  or roll back together. Conversion rewrites rows by position against the
  legacy value (as `replace_all` does), so re-converting a chat the previous
  release touched writes only the positions it changed. Valid legacy values
  are not rewritten (they decode equal). A legacy value that is not a JSON
  list keeps its exact bytes in `chat_transcript_damage`, written in the
  same transaction that replaces the chat's rows (and so its mirror) with a
  visible recovery placeholder.
- After every command the writer rolls back writes the command left
  uncommitted, so a no-op command never holds SQLite's write lock.

### Background conversion

- A background task (`chat_writer.convert_remaining_transcripts`) submits one
  `ConvertNextTranscript` writer command per chat in id order. It gives
  search its coverage and later writes their speed; nothing waits for it. A
  chat whose conversion fails is recorded (`/api/debug/status` →
  `transcript_conversion`, per chat) and skipped in that run. Its legacy
  value stays authoritative and readable, later chats still convert, and no
  route is blocked: only a write to that chat fails, with the error.
  Projections are total over arbitrary JSON, and bytes that are not a
  message list take the damage path, so such a failure (for example a
  `RecursionError` on deeply nested JSON) means a bug, which stays visible.
  Release 2's contraction requires the failed set to be empty (below).
- The disk rule. The background loop is deferrable bulk work. It must never
  itself push the volume into the critical tier, where agent admission
  defers every turn, so it stops one tier earlier, at the existing
  "constrained" verdict, checked before each chat. A per-chat byte bound
  against the critical floor would be the tighter rule, but it is not
  provable: an FTS5 insert can trigger an incremental merge whose output is
  proportional to the whole search index, not to the chat, and those pages
  sit in the WAL until a checkpoint. The stop is recorded; the existing
  capacity-monitor tick re-arms the loop when it observes disk pressure back
  to normal (no timer of its own), keeping each chat's failure record until
  it converts, and the next boot resumes it in any case (the marker table is
  its durable progress). Conversion inline in a write needs that one chat
  and is bounded by it, so it never consults the tiers; SQLite's own
  `SQLITE_FULL` is its bound. No reader is affected by disk tiers.
- Disk cost: conversion adds about 1x the converted chats' legacy transcript
  bytes (measured 1.09x: rows, search entries and indexes; the legacy column
  is kept). A rollback does not return it, and neither does release 2's
  column drop without a VACUUM. `/api/debug/status` states this beside the
  pending count.
- Once the loop leaves no chat unconverted, a per-process fact
  (`transcript_rows.conversion_settled`) ends the per-read marker check:
  nothing in this process can unconvert a chat, because its own mirror
  re-marks.
- The commit mirror's changed-chat set belongs to the root transaction: it
  survives a rolled-back savepoint and a failed, retried commit, and is
  cleared only when the root transaction ends (commit, rollback or close).

### Reads and search

- `transcript_rows.history(chat)` is a position-addressed view sized when
  opened; for rows, iteration streams one statement (one snapshot) and an
  index that has since vanished raises `IndexError`. For a converted chat,
  targeted reads (`at`, `client_message_seq`, `assistant_index`,
  `max_timestamp`, `metadata`, `attachment_bodies`) never decode ordinary
  bodies. Detail and log read owners pin one SQLite snapshot with
  `pin_read_snapshot`.
- Search reads only converted chats' message text: while `chats.messages`
  exists, a prose entry counts only for a chat with a marker, because the
  previous release may have replaced that transcript since the entry was
  derived. Titles are indexed and found for every chat. The search response
  carries `X-Search-Unindexed-Chats`, the number of chats not yet converted,
  and the shell shows a quiet note when it is above zero; there is no
  fallback read of the previous release's index.
- `chat_search_entries` (stripped title at seq -1, one row per prose item,
  FTS5) is maintained by triggers on `chat_messages` and `chats.title` for
  every writer. Search reads entry text as bytes and decodes with
  replacement, so an escaped lone surrogate cannot fail a query.
- Boot checks the transcript triggers (`schema_migrations.TRANSCRIPT_TRIGGERS`,
  one `sqlite_master` read) while `chats.messages` exists and reinstalls any
  that are missing from 0087's frozen DDL, logging it. When the detection
  trigger itself was missing, the same transaction clears every conversion
  marker, so each chat reads from and re-converts from `chats.messages`,
  which is exact in every case (this release's mirror or the previous
  release's newer write): a later table rebuild would otherwise silently
  stop detecting the previous release's writes. The repair also removes the
  rows, entries, damage copies and markers of chats deleted while
  `chats_deleted` was missing, and rebuilds every title entry. A test
  applies every migration and asserts the triggers remain.
  Search only reads, applying drawer visibility at query time.

### The next release's database

The previous release's NOT NULL column has no default, so `create_chat`
supplies a placeholder that the commit hook replaces; the ORM mapping has no
default of its own and is skipped by `mapped_schema_gaps` (column info
`legacy_transcript`). When the column is absent (`transcript_rows.legacy_present`
is false, after release 2 dropped it) this release reads and writes rows only,
so rolling back from release 2 to release 1 is safe. SQLite is the only
supported database; migration `0087_transcript_rows` refuses others.

## Release 2 (later)

- Refuse before any write when `chats.messages` exists and any chat lacks a
  marker (`transcript_conversion_incomplete`); the updater rolls back and
  release 1 finishes converting. So the contraction requires release 1's
  failed-conversion set to be empty: a chat that cannot convert must be
  fixed (or its failure understood) first.
- One migration: drop `chats_messages_written`, recreate `chats_deleted`
  without the marker and old-search lines, drop `chat_transcript_state` and
  the previous release's search tables, clear and drop `chats.messages`.
- Remove `chats_messages_written` from `TRANSCRIPT_TRIGGERS` (boot's guard then
  checks the permanent triggers always), and delete `legacy_present` and its
  branches, the mirror clause of the commit
  hook, the legacy branch of every reader (`legacy_messages`), `convert`, the
  conversion command and task, the placeholder supply, the `legacy_messages` mapping and its gap-check skip.
  `chat_transcript_damage` rows stay. Change `reflection-evidence.py` to count
  rows.
- The previous release refuses a release-2 database by its own schema check
  (missing mapped column) before writing anything.
- Plan space reclamation: dropping the column frees no file space until a
  `VACUUM`, which itself needs about the database's size free while it runs.
