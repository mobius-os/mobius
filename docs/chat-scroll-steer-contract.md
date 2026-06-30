# Chat scroll + steer contract

**This file is the source of truth** for how a chat scrolls when the owner
sends, queues, or steers a message, and how scroll position is restored on
leave/return. The lock-in Playwright specs below encode this contract; **if
code and a spec disagree, the spec is right and the code is the bug** — unless
this document says otherwise (the owner's words here outrank a stale
assertion, e.g. the "always reserve" clarification under R2).

Load-bearing implementation lives in `useScrollMode.js` (spacer + mode state
machine), `ChatView.jsx` (send/steer wiring), `usePendingQueue.js` +
`chatRuntimeState.js` (queue + steer gating). See also CLAUDE.md "Chat UX —
non-negotiable constraints", which this doc consolidates and refines.

## The scroll rules

### R1 — First message pins to the top
The first message in a chat **always** pins to the viewport top, with enough
bottom space reserved below it for the streaming response to grow into. The
user sees their message at the top and the answer growing beneath it, like a
terminal.

### R2 — Subsequent messages: always reserve, but only move when at the bottom
For every subsequent send, **always reserve enough bottom space** — but whether
the *scroll position moves* depends on where the reader is:

- **At the bottom** (the autoscroll/follow zone — within ~50px of the true
  scroll bottom): the new message pins to the top, response grows below.
- **Scrolled up (reading)**: the reader is presumed to be reading (possibly with
  something queued), so the send is a **no-op for scroll position** — the
  viewport does **not** jump. Space may still be reserved below; the
  load-bearing guarantee is that *the reader is not moved*, **not** that the
  spacer is zero. (Owner clarification 2026-06-30: "we always reserve enough
  space … if not [at the bottom] … we don't move the scroll position.")

"At the bottom" is decided from the scroll position captured **before** the new
message appends (`isNearScrollBottom`), not a raw IntersectionObserver read —
appending the assistant shell hides the bottom sentinel before the first
follow-write, so a sentinel read at send time misclassifies an at-bottom reader.
See `shouldPinSend` in `useScrollMode.js`.

### R3 — Steered messages follow the same rules
Fast-forwarding ("steer") a queued message into the live turn obeys R1/R2: pin
the steered row to the top only if it would have pinned as a fresh send (first
message, or the reader was at the bottom when they tapped fast-forward);
otherwise leave the reader anchored. The pin decision is recomputed at the
moment the steered row is committed, not when it was queued.

### R4 — Leave-and-return restores the exact position, even mid-stream
Leaving a chat and returning restores the **same scroll position** the reader
last saw — including while the agent is still streaming. On return: restore the
cached prior state first (same position), then apply any messages that arrived
since, without the viewport jumping. Implemented as hide-then-reveal in
`useScrollMode.js` (render `.chat__scroll` hidden, restore saved spacer +
`scrollTop`, wait for lazy renderers to settle, then reveal) plus the versioned
snapshot cache in `streamSnapshotCache.js`.

## Steer semantics — separate rows, one agent turn

When 2+ queued messages are steered into the live turn:

- They render as **separate user rows** in the transcript, in send order
  (`insertMessageBatchByTs` inserts each at its ts position so the reload order
  stays Q / A — never a steered row stranded after the agent's reply).
- The agent still receives them as **one steered turn**, but the combined
  content is joined with **`"\n\n"` (a paragraph break), not `"\n"`** — a
  steered multi-message turn should read as distinct messages, not a single
  newline-crammed blob. The separator must byte-match on both sides:
  `handleSteer` in `ChatView.jsx` (`steerTexts.join("\n\n")`) and
  `_selected_force_steer_pending` in `backend/app/routes/chats_stream.py`
  (`"\n\n".join(...)`), because the backend validates `body.content == expected`.
- The durable transcript rows are persisted **separately** (server-owned,
  rebuilt from `pending_messages`), independent of the joined content sent to
  the live turn.

The fast-forward ("Send queued message now") button only appears once **every**
queued row is server-confirmed (`serverTs === true`) and a turn is active
(`canFastForwardQueue`). A queued-message ack that returns only a server `ts`
(no canonical `pending_message`) still confirms the row — confirm on a numeric
ts, or the button never appears.

## Regression guards — the two observed prod bugs

These are the bugs the owner observed on prod (2026-06-30); the specs above must
keep catching them:

- **Bug A (R2):** a message sent while at the bottom landed in the *middle*, not
  the top (space under-reserved or `scrollTarget` wrong).
- **Bug B (R3/steer):** a steered message rendered at the *bottom*, after the
  agent's reply, instead of in transcript order. Fixed by `insertMessageBatchByTs`.

## Lock-in specs

| Spec | Locks in |
|------|----------|
| `tests/send-rule.spec.mjs` | R1, R2 (first-msg pin; at-bottom pin; scrolled-up no-move) |
| `tests/spacer.spec.mjs` | Spacer math + persistence |
| `tests/second-send-pin.spec.mjs` | Send-pin UX through the full SSE flow |
| `tests/steer-queued.spec.mjs` | R3 + steer button gating + separate-rows order + `\n\n` join |
| `tests/stream-reconnect.spec.mjs` | R4 (reconnect/restore), steer-on-reconnect |
| `backend/tests/test_chats_stream_steer.py` | Backend force-steer match (`\n\n`) + separate durable rows |
