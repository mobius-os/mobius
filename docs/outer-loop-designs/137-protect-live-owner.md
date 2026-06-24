# 137 — Protect the live owner from dev-loop churn

Status: 1b READY · 1a + 1c IDEA (open design)  ·  Priority: p1 (owner-facing)
Backlog tracker: `.pm/features/137-protect-live-owner.md`

Adversarially reviewed against the real code (Codex + Claude cross-card,
2026-06-24). The review **corrected several wrong premises in the first draft** —
those corrections are folded in below and called out as `[review]`.

## Why

~80 parallel dev-agent sessions ship to ONE shared prod instance the owner uses
live. Prod chats (owner's words, last 48h): "why does the server keep restarting
every 5 seconds"; "another agent was restarting the server, and at the same time
I got a 502 … my last message and the 502 were gone"; "queued messages … lost
all progress"; recurring "logged a gym session, told it saved, never actually
saved." The dev loop degrades the owner's live experience — and "breaking is
allowed + recovery exists" is the philosophy for the *agent's* substrate edits,
not for the *human owner's* typed input + a claimed save (that's a durability
contract → "invisible robustness for the platform").

## What the code ALREADY does (so we don't redesign it) `[review]`

- **The inbound user message is already durable before the agent spawns.**
  `send_message` submits `StartTurn`, awaits the ack, THEN creates the broadcast
  and schedules `run_chat`. `StartTurn` appends the user message, sets
  `run_status`, inserts `ChatRun`, and commits before returning
  (`routes/chats_stream.py:631,646,651`; `chat_writer.py:1292,1296,1307,1312`).
  So "persist the message before processing" is NOT a gap — keep it + add a
  regression harness.
- **`reconcile_interrupted_chats` does NOT replay** — it appends a visible
  interrupted-turn error and clears `pending_messages`; it does not respawn agent
  work at boot (`chat.py:713,728,731`; `main.py:91,105`). The first draft's
  "replay, don't drop" premise was the opposite of reality.
- **SSE catch-up is process-memory only.** After a restart `get_broadcast`
  returns None → `/stream` returns 204 → the frontend falls back to a DB refresh
  (`broadcast.py:74,126`; `chats_stream.py:741,752`;
  `useStreamConnection.js:442`). There is no durable cross-restart event replay.
- **Storage writes are durable before the 204** — `atomic_write` does
  fsync + os.replace before the route returns (`routes/storage.py:669,689`;
  `storage_io.py:57,60,62`). The gym "false save" is NOT a storage-route
  commit-before-ack bug; investigate it separately (see "Spun-off" below).

## 1b. Presence-gate  — READY (small, concrete, ship first)

Before a restart/recreate, check whether a live turn is in flight and DEFER (or
require an explicit `--force-now`), advisory-by-default.

- **Signal:** the union of `active_sdk_clients` + `active_sdk_sessions` +
  `starting` + running broadcasts from `GET /api/debug/status` (owner-auth;
  `routes/debug.py:74-76`). The `starting` set is load-bearing — it covers the
  window between the `StartTurn` commit and SDK-handle creation, the exact TOCTOU
  the gate must not miss `[review]`.
- **Where (the real restart paths, not the names in the first draft) `[review]`:**
  - `scripts/deploy-prod.sh` — before BOTH the cutover (`up -d`) and the
    post-shell `docker restart` (`deploy-prod.sh:655,677,748,755`).
  - `POST /api/admin/restart` (`routes/admin.py:217,221`) — the normal Settings
    restart (NOT `/api/platform/restart`).
  - `/recover/restart` (`recover_chat.py:390`) — decide separately: likely WARN,
    not block (recovery must always be reachable).
- **Philosophy:** advisory + defer-by-default + visible logging + explicit
  `--force-now`; never an irreversible mutex over the owner's own operations
  `[review]` (CLAUDE.md "code empowers, does not police").
- **Caveat:** crash-loops + OOM bounces bypass any gate — 1b is politeness, not a
  guarantee. The guarantee is 1a + 1c.

## 1a. Restart-survivable in-flight chat — IDEA (real design, ensemble)

The genuine remaining problem, narrowed by the review:

1. **The "lost message" path.** Inbound is durable *once `StartTurn` commits* —
   so a message "gone" after a 502 means the POST 502'd BEFORE the commit (a
   restart/proxy bounce mid-request). Fix is frontend-side: preserve the
   optimistic message + retry the POST on a 502/network drop instead of dropping
   it. Cheap, high-value, and independent of the backend.
2. **The mid-turn UX decision** (the design fork the review flagged as a
   blocker): a restart mid-turn currently yields a visible interruption + the
   user resends. Two options:
   - **(a) Keep "visible interruption + resend"** and make it CLEAN: the user's
     message survives (it does), the partial assistant snapshot survives (the
     actor coalesces it), and the UI shows "interrupted — resend" rather than
     frozen dots or a vanished message. Cheapest; probably enough.
   - **(b) True durable replay**: a durable event log with a cursor/sequence so
     `/stream` can resume across a restart, and reconciliation re-spawns pending
     turns. Much bigger; only if (a) proves insufficient.
   Recommend (a) for v1; revisit (b) only if owners still report loss.
3. Acceptance becomes: restart at any point and the owner's MESSAGE always
   survives, the partial output survives, and the state is visibly resumable —
   NOT "the turn auto-resumes" (that's (b)).

Risk: persistence/streaming/concurrency → true Claude+Codex ensemble +
adversarial per CLAUDE.md. Do not solo. Own it in its own session.

## 1c. Staging for the fleet — IDEA (the structural lever, host-constrained)

Make the fleet default to a staging target; prod requires explicit `--to-prod`.

`[review]` **Do NOT add a second long-lived staging container on the same
~16GB host** — it already OOM-bounces during builds + cutover
(`deploy-prod.sh:43,52`; `.pm/116`). Prefer **remote staging** or **ephemeral
per-slug test containers** (the discipline already exists) with explicit
concurrency + memory budgets. Promotion staging→prod = the normal
`deploy-prod.sh` from main (unchanged). This is the highest-leverage fix because
it removes the coupling rather than mitigating it — but it needs a real
resource budget before any same-host option.

## Spun-off bug (not 1a): the gym false-save

The "saved but not persisted" report is NOT the storage route. Investigate: the
gym app's own client/store write path, and/or the agent's natural-language
"saved" claim AFTER a tool call that didn't actually persist. Fix = read-
after-write verification in the app + holding the durability guarantee on the
nightly transcript-read, not on the daytime claim. File as its own card.

## Done when
- [ ] 1b: union-presence-gate in deploy-prod.sh (both restarts) + `/api/admin/restart`,
      advisory + `--force-now`; recovery restart warns only.
- [ ] 1a: frontend preserves+retries an interrupted POST; mid-turn UX is a clean
      visible-interruption-resend (option a) with message + partial surviving;
      restart-during-turn harness proves no message loss.
- [ ] 1c: fleet defaults to remote/ephemeral staging with a memory budget; prod
      needs `--to-prod`; promotion documented.
- [ ] Prod chats stop showing "restart every 5s" / "my message was gone."
