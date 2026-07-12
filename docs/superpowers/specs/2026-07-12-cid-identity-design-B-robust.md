# Fix-Forward Redesign: One Message Identity (`cid`), Minted Once, Carried Everywhere

**Framing: most robust.** The goal is a single identity for a user message that is
correct under every race, reload, multi-tab interleaving, steer/stop combination, and
SSE replay. Where a simpler version would break, that break is called out inline.
Read-only analysis of `origin/main` tip `8ee77cc2` in the `chat-identity` worktree; no
files were modified.

---

## 0. The disease, stated precisely

A user message's identity currently rides on `ts`, and `ts` is **not stable**:

- The client renders an optimistic user row keyed by `ts = Date.now()` (compose time).
- The server assigns a canonical `ts = int(time.time()*1000)` (`_user_message_from_body`,
  `chats_stream.py:226`), then `_ensure_unique_ts` may bump it `+1ms`
  (`chat_writer.py:2004`) to keep it unique within the queue.
- **The two `ts` values diverge, and which one the DOM row carries depends on the send
  path.** Fresh sends keep the optimistic `ts` forever (ChatView `~1943` comment:
  "pin to the OPTIMISTIC ts … never reconciles … the divergence survives reload").
  Queued/steered rows swap to the server `ts` (`swapOptimisticTs`).

Because `ts` is simultaneously (a) the DOM pin selector key
(`.chat__msg--user[data-ts="…"]`), (b) the React row key, (c) the queue-cancel key
(`DELETE /pending/{ts}`), (d) the steer-consume key (`consume_pending_ts`), and (e) the
steer-dedup key (`(ts,content)`), every one of those five subsystems had to grow a
compensation for the swap. **A frontend-only half-identity already exists** — `cid`
(fresh UUID for optimistic queue rows, `s-<ts>` for hydrated) — and it is exactly the
right shape. It just stops at the network boundary (confirmed: no backend code reads a
`cid`, `streamSend` never puts it on the wire).

**The fix is structural, not a patch:** promote `cid` to the one canonical identity,
mint it once at compose, carry it across the wire and into persistence, key the DOM /
pin / queue-cancel / steer / dedup on it, and demote `ts` to pure display+ordering
metadata that may update in place under a stable key. Once identity no longer rides on
`ts`, **the optimistic row and the canonical row become the same React element** (same
`cid` key) — the ts-swap that broke pins simply cannot happen, and ~350–450 lines of
swap-compensation delete.

---

## 1. Identity lifecycle

### 1.1 Minting

`cid` is minted **once, at compose time, for every visible user send**, in `doSend`
(ChatView.jsx). Today only the queue path mints one (`~1571`); the fresh path uses
`{optimistic:true}` with no cid. Unify: mint at the very top of `doSend`, before the
queue-vs-fresh branch, so both paths carry the same identity:

```js
const cid = (crypto?.randomUUID?.())
  ?? `cid-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`  // kept fallback
```

Scope: **user rows only.** Assistant messages need no `cid` (pins never target them;
ANCHOR_AT keeps keying on `data-key = msg.id || role-ts`). Hidden answer sends
(`doSendSilent`) render no visible row and carry no `cid`. `cid` is a
**rendering/dedup key, never an auth or ownership boundary** — the server treats it as
untrusted client input (see §5 multi-tab / duplicate-cid).

### 1.2 Wire format

Add one optional field, `cid: str | None`, to `schemas.SendMessage`
(`schemas.py:313`). The client sends it on every POST `/messages` (fresh, queued,
force-steer). `streamSend` (`useStreamConnection.js:~1122`) adds `if (cid) body.cid = cid`.

The server **echoes** the canonical row's `cid` in every payload that today echoes a
message or a ts:

| Payload | Field carrying `cid` today → after |
|---|---|
| 202 `{"status":"started","message":…}` | `message.cid` |
| 202 `{"status":"queued","pending_message":…,"ts":…}` | `pending_message.cid` |
| 202 `{"status":"steered","messages":[…]}` (`_steered_response`) | each `messages[].cid` |
| SSE `queued_turn_starting` `message._messages[]`, `message._consumed_ts` | `_messages[].cid`, **new** `message._consumed_cids` |
| SSE `steered_into_turn` `messages[]` | each `messages[].cid` |
| GET `/chats/{id}` `messages[]`, `pending_messages[]` | each row's persisted `cid` |

### 1.3 Persistence shape

Both `Chat.messages[]` and `Chat.pending_messages[]` user rows gain a `cid` string:

```
{ "role": "user", "content": "...", "ts": 1720000000000, "cid": "3f2a…",
  "hidden"?: true, "attachments"?: [...], "timezone"?: "...", "viewport"?: {...} }
```

`cid` is written wherever a user row is first persisted:

- `_user_message_from_body` (`chats_stream.py:218`) copies `body.cid` onto the dict.
- `StartTurn._start_turn`, `AppendPending._append_pending`,
  `AppendSteeredUserMessage._append_steered_user_message` persist the `user_msg`/`user_msgs`
  dicts that already carry `cid` from the route.
- **Preservation invariant:** every place that does `dict(pending_msg)` /
  `dict(raw_msg)` and pops UI-only keys must **keep `cid`**. Specifically
  `_pending_messages_for_transcript` (`chat_writer.py:2076`) pops
  `queued`/`serverTs`/`position`/`_initiated_by_app_id` — it must NOT pop `cid`.
  `_combine_pending_messages` (`~2020`) spreads `**first`, so `cid` rides on the
  agent-facing combined row; that is harmless (the provider never sees it) and lets the
  `_consumed_cids` echo reference the head row's identity.
- **`_ensure_unique_ts` touches only `ts`, never `cid`.** This is the crux: bumping the
  display ts leaves identity untouched.

### 1.4 Lazy legacy derivation (fix-forward, no migration)

Old rows persisted before this change have no `cid`. Derive one **at read time**,
deterministically from `ts`:

```
cid_of(row) = row.cid ?? `legacy-${row.ts}`
```

This is applied at exactly two boundaries so a derived id is identical on both sides of
every comparison:

- **Frontend read:** the GET `/chats` normalizer in ChatView (`~1338` block that
  already walks `msgs` normalizing tool status) stamps `msg.cid ??= 'legacy-'+msg.ts`
  on user rows; `usePendingQueue._fromServerList` changes `m.cid || 's-'+m.ts` →
  `m.cid || 'legacy-'+m.ts`; `hydrate` derives the same.
- **Backend echo:** when the server builds `started_messages` / `steered_into_turn`
  rows / `_messages` from legacy rows lacking `cid`, it fills `legacy-<ts>` so the wire
  value matches what the client would derive.

Because `ts` is already unique within a chat (via `_ensure_unique_ts` at write time),
`legacy-<ts>` is collision-free and stable across reloads. Legacy rows are rarely
re-steered/cancelled, but if they are, cid-keyed ops work because the derivation is
pure.

---

## 2. Exact surface changes (file-by-file) — **the deletion list is the point**

### 2.1 `frontend/src/components/ChatView/useScrollMode.js`

- **ScrollMode union: `PIN_USER_MSG{ts}` → `PIN_USER_MSG{cid}`.** Every doc block
  listing the union (`~10`, `~364-384`, the `@returns` JSDoc `~415-424`) updates.
- `_pinnedUserEl(scrollEl, ts)` → `_pinnedUserEl(scrollEl, cid)`: selector becomes
  `.chat__msg--user[data-cid="${cid}"]` (via `CSS.escape`).
  **DELETE the last-row fallback** (`~120-121`, the whole `rows[rows.length-1]`
  block and its long comment). With `cid` stable from mint, the optimistic row already
  carries its final `cid`, so the exact selector always resolves. The fallback existed
  *solely* to paper over the ts-swap ("a fresh send … never reconciles that data-ts …
  so an exact-ts lookup finds nothing"). That reason is now gone.
- `applyMode` PIN case, `_pinReapplyNeeded`, `settlePinnedMode`, `maybeApplyMode`,
  `syncLayout` viewport branch: all read `mode.cid` instead of `mode.ts`. Pure rename.
- `_validateSavedMode` PIN branch (`~173-176`): compare `lastUserMsg.cid === saved.cid`.
  **Legacy sessionStorage fallback:** a saved `{kind:'PIN_USER_MSG', ts}` from a
  pre-change session has no `cid` → degrade to `FOLLOW_BOTTOM` (sessionStorage is
  ephemeral and self-heals on the next send). Do not attempt a ts match — keep the
  degradation path trivial.
- `sizeSpacer`'s `_pinnedUserEl(scrollEl, null)` fallback call (`~592`) stays (it
  passes `null` to mean "last user row in DOM" for spacer geometry, which is a
  legitimate geometry fallback independent of pin identity — but see note: with the
  identity fallback removed from `_pinnedUserEl`, keep a dedicated
  `_lastUserRowEl(scrollEl)` helper for this one geometry caller so the pin selector
  stays strict). This is a **1-line split**, not a behavior change.

### 2.2 `frontend/src/components/ChatView/ChatView.jsx`

- **Add the pin funnel `pinSentMessage(cid, {willPin, intent})`** (§3). 
- **Collapse all seven pin-apply sites** to a single `pinSentMessage(...)` call each
  (send/continuation `~880`, steer `~1010`, queued-promote A `~1665`, queued-promote B
  `~1725`, fresh-send `~1835`, fresh-send-queued retarget `~1905`, fresh-send-started
  retarget `~1945`). Each site today hand-rolls `setSpacerActive(true)` +
  `modeRef.current = {PIN_USER_MSG, ts}` / `settleNonPinMode`; the funnel owns all three.
- **DELETE the two retarget branches entirely.** With `cid` stable from mint there is
  no server-`ts` to retarget to: the fresh-send-started site (`~1941-1964`) pins to
  `{PIN_USER_MSG, cid}` directly and **the whole "pin to the OPTIMISTIC ts because the
  row never reconciles" comment + logic dies** (`~1943-1961`). The
  fresh-send-queued-started retarget (`~1898-1916`) similarly reduces to a funnel call.
- **DELETE the ts-keyed pin-intent plumbing:** `queuedPinIntentByTsRef`,
  `rememberQueuedPinIntentTs` (`~665`), `forgetQueuedPinIntent`'s `ts`/`tsList` params,
  and the `tsList`/`ts` keys inside `takeQueuedPinIntent` (`~679-698`). Pin intent is
  keyed **by `cid` only** now (`queuedPinIntentByCidRef` stays; it is already
  cid-keyed). This removes `rememberQueuedPinIntentTs` calls at `~1624`, and every
  `forgetQueuedPinIntent({ts, tsList})` collapses to `{cid}`.
- `findOptimisticUserIndex(messages, ts)` (`~165`) → `findUserIndexByCid(messages, cid)`.
- `replaceOptimisticWithBatch(prev, optimisticTs, rows)` (`~158`) → filter by `cid`.
- The optimistic user row is now `{role:'user', content:text, ts:Date.now(), cid}`
  (drop the `optimistic:true` marker — it was only read by `serverSnapshotBehindLocal`;
  see §4). data-`ts` still rendered for the timestamp tooltip; identity is `cid`.
- `handleStop` resend: the combined follow-up is a **new** logical message → **mint a
  fresh `cid`** (do not reuse the cleared rows' cids). `cleared_pending_ts` handling in
  the Stop response becomes `cleared_pending_cids` (§5 Stop-resend).
- `handleSteer`: `consume_pending_ts`/`steerTexts.join` → `consume_pending_cids`; the
  content byte-match requirement disappears (see backend `_selected_force_steer_pending`
  below), so the fragile "must byte-match `steerTexts.join('\n\n')`" contract
  (`~2445-2454`) relaxes to "send the cids".
- **Message row renderer** (`~3030-3037`): `dataKey` stays `msg.id || role-ts` for
  ANCHOR; **add `data-cid={msg.role==='user' ? cid_of(msg) : undefined}`** as the pin
  selector; React `key` for user rows becomes `cid_of(msg)` so the row **never
  remounts** across the ts-in-place update (this is what kills the remount that
  `QueuedMessages.keyOf` had to work around, now made uniform). `data-ts` stays (drives
  the timestamp `<time>` tooltip only).

### 2.3 `frontend/src/components/ChatView/hooks/usePendingQueue.js`

This file shrinks the most — it is where the ts-swap identity system was reinvented.

- `_fromServerList`: `cid: m.cid || 'legacy-'+m.ts` (was `'s-'+m.ts`).
- **`swapOptimisticTs` → `confirmQueued(cid, {ts, position, serverMsg})`**: it no longer
  *swaps identity*. It updates the display `ts`, `position`, `serverTs` flag, and merges
  `serverMsg` fields on the row matched **by cid** (which never changes). 
- **DELETE the entire `consumedServerTsRef` deferred-removal guard** (`~111-133`,
  `resolveCidFromGuard` `~166-175`, the arming logic in `promoteManyByTs` `~336-345`,
  and every `resolveCidFromGuard`/`consumedServerTsRef` reference). Its whole reason for
  existing — "a consumed server ts whose OPTIMISTIC entry is still in flight (carries a
  client ts, not this server ts) can't be removed by ts" plus the reissued-ts **bug #4**
  machinery — evaporates: promote/consume is now **by cid**, and the optimistic row's
  cid is final from mint, so removal is immediate and unambiguous. This is ~90 lines.
- **DELETE `swapOptimisticTs`'s reissue branches** (`~218-238`): the "was this cid in
  the armed set?" and "already-hydrated twin by ts" special cases are gone.
- **DELETE the content-identity heuristic in `hydrate`** (`_queueIdentityKey`
  `~91-96`, `serverByIdentity`/`localInFlightByIdentity`/`cidByServerTs`/
  `matchedInFlightCids` `~448-484`). That heuristic existed only because **there was no
  shared id across the wire** — hydrate had to guess which local optimistic row a server
  row corresponded to by matching trimmed-content+attachments. Now the server row
  carries the same `cid` the client minted, so hydrate matches **by cid** directly:
  reuse the local row (keep its expanded-tray UI state) when `server.cid === local.cid`,
  else it's a genuinely new server row. ~70 lines.
- `promoteByTs`/`promoteAll`/`promoteManyByTs` → **`promoteByCid`/`promoteManyByCid`**
  (select by `cid`, not `ts`). `promoteAll(ts)` (head-onward) keys on the head cid.
- `cancelByTs` stays as a thin convenience (DELETE-by-cid is primary; see §5), but its
  in-flight cleanup simplifies (no guard to prune).
- `inFlightCidsRef` **stays** — it is already cid-keyed and still guards the genuine
  "optimistic POST racing a reconcile read" race (§5 reconcile-clobber). It is
  orthogonal to identity and correct.

### 2.4 `frontend/src/components/ChatView/QueuedMessages.jsx`

- `keyOf(msg)` (`~32`) becomes `msg.cid` unconditionally (drop the `t-<ts>` fallback —
  every row now has a cid, derived if legacy). This file was **already the one place
  identity was right**; it now just loses its defensive fallback. `onCancel?.(msg.ts)`
  (`~135`) → `onCancel?.(msg.cid)`.

### 2.5 `frontend/src/components/ChatView/chatRuntimeState.js`

- **DELETE `resolveFreshPinRetarget`** (`~103-121`) — no ts to retarget; the funnel's
  intent-staleness check subsumes it.
- **DELETE / collapse `resolveSteeredPinDecision`** (`~86-101`) — the `pinTargetTs`
  computation dies; what remains ("intent still current? willPin?") is exactly the
  funnel's own logic, so the helper folds into `pinSentMessage`.
- `startedMessagesFromResponse` / `continuationRowsFromPromotedMessage` /
  `stripInternalUserMessageFields` stay, but `stripInternalUserMessageFields` (`~6`)
  must **keep `cid`** (it currently strips `cid` as an internal field — that was correct
  when cid was frontend-only; now cid is the durable identity and must survive the strip
  that prepares a server row for the transcript). Also add `_consumed_cids` to the
  internal-fields strip list alongside `_consumed_ts`.
- `serverSnapshotBehindLocal` (`~36`) stays but its `m.optimistic === true` clause is
  replaced — since the optimistic marker is dropped, the "local is ahead" signal is
  "the row's cid is absent from the server snapshot AND the row is `queued`/`serverTs===false`".

### 2.6 Backend

**`routes/chats_stream.py`:**
- `_user_message_from_body` (`~218`): copy `body.cid` onto the row.
- `_selected_force_steer_pending` (`~363`): select pending rows by
  `body.consume_pending_cids` (matched against each pending row's `cid`), and
  **DELETE the content byte-match** (`~386-392`, the `expected == body.content` check).
  That byte-match existed only to bind the steer request to specific queued rows in the
  absence of a shared id; cid does that directly and robustly (no `\n\n`-join fragility).
- `_queued_response` / `_steered_response` / the `started` 202 payloads: include `cid`.
- The `steered_into_turn` broadcast (`~780-795`): each `messages[]` row carries `cid`.
- `DELETE /pending/{ts}` → **`DELETE /pending/{cid}`** (`~962`). `CancelPending`
  (below) removes by cid. This is the clean removal of "`_ensure_unique_ts` bumps
  colliding ts +1ms INSIDE the queue so DELETE stays unambiguous" — DELETE is now
  unambiguous by construction.

**`chat_writer.py`:**
- `CancelPending` command: match by `cid` not `ts`.
- `_ensure_unique_ts` (`~2004`): **unchanged code, but its identity role is deleted.**
  It stays as pure **ordering/collision-avoidance for the display `ts`** (so two
  same-ms sends still sort deterministically). Its docstring's "duplicate React keys /
  ambiguous DELETE-by-ts" justification is rewritten — React keys are now `cid`, DELETE
  is by `cid`. (Could be dropped entirely if ordering is taken over by insertion order,
  but keeping monotonic display ts is cheap and preserves the timestamp tooltip's
  sanity; recommend keeping it, demoted.)
- `_append_steered_user_message` (`~1408`): **replace the `(ts,content)` dedup**
  (`_steer_dedup_key` `~1459`, `seen_keys` `~1470`) with **cid dedup** — a re-delivered
  force-steer of the same queued row carries the same `cid` and is dropped; genuinely
  distinct sends carry distinct cids. This is strictly more correct: the old
  `(ts,content)` key could **collide two genuinely distinct sends** with identical text
  (yesterday's steered-twice bug was a real duplicate *disguised as distinct* by the
  +1ms bump; cid removes both the disguise and the false-collision risk). `consume_pending_ts`
  → `consume_pending_cids`.
- `_pending_messages_for_transcript` (`~2076`): keep `cid` through the pop.
- `_promote_pending` (`~1536`): the returned `_consumed_ts` becomes `_consumed_cids`
  (the head row's `_combine_pending_messages` combined row still carries the head cid);
  `_pending_messages_for_transcript` rows carry their cids.

**`claude_sdk_runner.py` / `codex_sdk_runner.py`:** the steer buffer
(`_steer_user_msgs`, `~210`) dedup-by-queued-`ts` (`~267`, `buffered_ts`) becomes
**dedup-by-`cid`**. `consume_pending_ts` buffered on the handle → `consume_pending_cids`.
`chat.py` `split_for_steer` (`~413`) passes cid-carrying rows through unchanged (it
already forwards `user_msg`/`user_msgs` dicts).

**`schemas.py`:** `SendMessage` gains `cid: str | None`, `consume_pending_cids:
list[str] | None` (replacing `consume_pending_ts`; keep `consume_pending_ts` accepted-
but-ignored for one release only if any non-browser caller exists — none found, so it
can be removed outright per the fix-forward mandate).

### 2.7 `chatContract.js`

Geometry predicates (`pinLanded`, `pinHeld`, `cushionPresent`, etc.) are **unchanged** —
they take numeric snapshots, not identities. Only the **injection harness** that reads
`data-ts` to locate the pinned row switches to `data-cid`. `PIN_OFFSET`/`PIN_BOTTOM_ROOM`
mirrors stay.

---

## 3. The pin funnel

```js
// ChatView.jsx — the ONE place a send/steer/promote arms the pin.
// Owns: spacer arming, intent-staleness (a user scroll after submit wins),
// and mode assignment. Every one of the former seven sites calls this.
function pinSentMessage(cid, { willPin, intent } = {}) {
  setSpacerActive(true)                       // reservation is always armed on a send
  if (intent && !pinIntentStillCurrent(intent)) {
    // The user scrolled (pointer/wheel/touch/key) after submit — their intent
    // is newer than this delayed pin. Leave scrollTop where they put it; the
    // spacer still reserves room below the new row.
    return
  }
  if (willPin && cid != null) {
    modeRef.current = { kind: 'PIN_USER_MSG', cid }   // cid, not ts
  } else {
    // Not pinning: retire any stale PIN_USER_MSG to the reader's ANCHOR_AT so a
    // later scroll-to-tail still has reserved room, without moving them now.
    settleNonPinMode(modeRef, scrollRef.current, { retireFollow: true })
  }
}
```

**Signature:** `pinSentMessage(cid, { willPin, intent })`.
- `cid` — the stable identity of the just-sent row (from mint, or the first promoted
  row's cid for a continuation batch).
- `willPin` — the at-submit-time decision (`shouldPinSend(...)`, computed at each call
  site from `wasNearContentBottomAtSubmit`/`isFirstUserMsg` exactly as today).
- `intent` — `makeSendPinIntent(willPin)`, carrying `userScrollIntentVersion` captured
  at submit. `pinIntentStillCurrent` compares it to `userScrollIntentVersionRef.current`;
  a human scroll after submit advances the version and **the scroll wins** (Chat-UX
  constraint #1, "a real user scroll after submit … wins").

**What it owns (previously scattered across 7 sites):** `setSpacerActive(true)`
arming, the intent-staleness gate, and the PIN-vs-settle mode write. The `spacer height
belongs to the layout effect; do not zero it here` comment (repeated at every site) is
stated once in the funnel.

**How the seven sites reduce:** each site keeps only its *domain* work (append the
rows, set `sending`, reset the build rail) and ends with **one** call, e.g.:
```js
const promotedCid = cid_of(startedMessages?.[0]) ?? cid
pinSentMessage(promotedCid, { willPin: queuedWillPin, intent: queuedPinIntent })
```
The retarget branches (fresh-send-started `~1941`, fresh-send-queued-started `~1898`)
**delete entirely** — there is no ts to retarget; `pinSentMessage(cid, …)` with the
mint-time cid is correct on the first apply because the DOM row already carries that cid.

**Optional tighter encapsulation:** expose `pinSentMessage` from `useScrollMode` as
`armSend({cid, willPin, intent, setSpacerActive})` so `modeRef` never leaks to
ChatView. Recommended but not required; the ChatView-local funcion above already
collapses the seven sites and is the smaller diff. Either way there is exactly **one**
funnel.

### Saved-mode persistence migration

`persistMode` writes `modeRef.current` (now `{kind:'PIN_USER_MSG', cid}`) to
`sessionStorage['chat-mode']`. `_validateSavedMode`:
```js
if (saved.kind === 'PIN_USER_MSG') {
  const lastUser = [...messages].reverse().find(m => m.role==='user' && !m.hidden)
  const savedCid = saved.cid ?? (saved.ts != null ? null : undefined)
  return (savedCid != null && cid_of(lastUser) === savedCid)
    ? { kind:'PIN_USER_MSG', cid: savedCid }
    : { kind:'FOLLOW_BOTTOM' }     // legacy {ts}-only save, or dangling → degrade
}
```
A pre-change session that saved `{ts}` has no `cid` → degrades to `FOLLOW_BOTTOM`
(ephemeral, self-heals on the next send). No migration of stored data needed —
consistent with the fix-forward mandate.

---

## 4. Contracts that must keep holding + test routing

### Contracts preserved (verbatim behavior, cid substituted for ts)

- **Chat UX non-negotiables #1–#9** (CLAUDE.md). The funnel preserves: fresh sends
  always pin (#1), reserve-on-send spacer independence (#1), no-scroll on not-at-bottom
  mid-turn send (#1/#3), re-anchor on promote (#3), `fullViewH` spacer sizing (#4),
  single assistant surface (#9). The only mechanical change is the pin selector key.
- **Message queue + send-while-generating:** append/cancel/promote still serialized by
  the per-chat `asyncio.Lock` + single-writer actor. Cancel is now cid-keyed; the
  actor's lost-update protection is unchanged.
- **Stop-chat contract:** two-layer Stop semantics intact; the resend mints a fresh cid
  and the cleared-set becomes cid-based (§5).
- **AskUserQuestion, provider lock, single-writer actor guardrails:** untouched
  (answers carry no visible row, so no cid).

### Tests: change vs die vs stay

**Change (selector/key rename, behavior identical):**
- `__tests__/useScrollMode.test.js` — the `data-ts="123"` cases (`~131-190`, `~311-338`)
  → `data-cid`. `PIN uses the exact data-ts match` (`~311`) becomes the exact-cid match.
- `tests/second-send-pin.spec.mjs`, `tests/pin-clamp-settle.spec.mjs`,
  `tests/spacer.spec.mjs` (esp. #26), `tests/send-rule.spec.mjs` — pin locators
  `data-ts` → `data-cid`.
- `__tests__/optimisticSteerQueue.test.js`, `tests/steer-queued.spec.mjs` — cid-keyed
  promote/consume.
- `tests/handleStop-sync-ordering.spec.mjs`, `__tests__/resolveStopResend.test.js` —
  `cleared_pending_cids`.
- backend `test_chats_stream_steer.py` — force-steer selection by cid, no content
  byte-match; `test_chat_writer.py` — steer dedup asserts on cid.

**Die (they test a compensation that no longer exists):**
- `__tests__/useScrollMode.test.js`: `applyMode PIN falls back to the last user row
  when the mode ts matches no data-ts` (`~289`) — the fallback is deleted.
- `__tests__/chatRuntimeState.test.js`: any `resolveFreshPinRetarget` /
  `resolveSteeredPinDecision` cases (helpers deleted).
- Any `usePendingQueue` unit test covering the `consumedServerTsRef` reissued-ts bug #4
  or the `swapOptimisticTs` twin-collapse — the guarded paths are gone.

**Stay (identity-agnostic):**
- `__tests__/chatContract.test.js`, `__tests__/serverSnapshotBehindLocal.test.js`
  (clause tweak only), `__tests__/streamPromotion.test.js`,
  `__tests__/streamReducers.test.js`, `stream-reconnect.spec.mjs`,
  backend `test_chat_writer_activation.py` / `test_chat_writer_contention.py` /
  `test_chat_queue_module.py` (lost-update + actor contracts, orthogonal to identity).

**New tests to add** (robustness coverage, §5): cid stable across optimistic→confirm
(no remount); hydrate matches by cid; legacy `legacy-<ts>` derivation identical
frontend/backend; multi-tab duplicate-cid idempotent append; Stop-resend mints fresh
cid; steer dedup by cid drops re-delivery but keeps identical-text distinct sends.

---

## 5. Risks and edge cases (enumerated failure modes; where the simple version breaks)

**Reload mid-turn (SSE catch-up).** The persisted transcript already carries `cid`
server-side, so GET `/chats` returns cid-bearing rows; the DOM keys on them and the pin
survives reload — **fixing the exact bug the old comment lamented** ("the divergence
survives reload"). `steered_into_turn` replayed during catch-up carries cid; the
catch-up dedup (`useStreamConnection ~912`) is unchanged (it drops the replayed
pre-steer segment). *Simple version breaks:* if cid were minted client-side only and not
persisted, a reload would lose it and the pinned row would fall back to a derived id
that the pre-reload session's sessionStorage `{cid}` mode wouldn't match — hence cid
**must** be durable server-side, not just echoed.

**Multi-tab.** Two tabs compose independently → distinct UUID cids; no collision. A row
queued in tab A appears in tab B via `hydrate`, keyed by its server-persisted cid. *Duplicate-cid
defense:* the server treats `cid` as untrusted. `AppendPending` should treat an incoming
`cid` that already exists in `pending + messages` as a **duplicate POST retry** →
return the existing row instead of appending a twin (free idempotent-POST robustness).
If a hostile/legacy duplicate cid still lands, DELETE-by-cid removes all matches
(identical intent) — acceptable, never a security boundary.

**Stop-resend (`handleStop`).** The combined follow-up is a new logical turn → **mint a
fresh cid**; do not reuse the cleared rows' cids (reuse risks a straggler steer-dedup
drop or a hydrate re-match against a row the user believes is gone). The backend's
`cleared_pending_ts` becomes `cleared_pending_cids`; `resolveStopResend` narrows the
resend to the cid-set the backend confirms it cleared (same PM-115 natural-finish-races-
Stop protection, now cid-exact instead of ts-exact). *Simple version breaks:* reusing a
cleared cid on resend could collide with a concurrently-draining continuation that still
references it.

**Steer dedup semantics.** Dedup by cid is *stronger and safer* than `(ts,content)`: it
drops a true re-delivery (same cid) while never false-merging two distinct sends that
happen to share text (the old `(ts,content)` key relied on the +1ms ts bump to keep them
distinct — the very bump that "disguised a real duplicate as a distinct message"). Both
the runner buffer and `_append_steered_user_message` dedup on cid; defense-in-depth
preserved.

**Server echo on 202 vs stream (ack/replay race).** A POST ack may be lost while the
`steered_into_turn`/`queued_turn_starting` SSE still arrives (or vice-versa). Because
both carry the **same cid**, whichever lands first reconciles the optimistic row and the
other is idempotent (matched by cid, mutates nothing new). *Simple version breaks:* with
ts-only, a lost ack + delivered SSE left the row on the optimistic ts while the queue
held the server ts → the twin-row / duplicate-fast-forward bug that `swapOptimisticTs`'s
twin-collapse branch existed to patch. cid makes it a no-op.

**Legacy chats.** `cid_of(row) = row.cid ?? 'legacy-'+row.ts` on both wire sides;
deterministic from a within-chat-unique ts, so pin/cancel/steer/dedup all work. A legacy
row that gets force-steered carries `legacy-<ts>` as its `consume_pending_cids` entry;
the backend matches it against the same derived value. No migration; old rows just
acquire a stable derived identity at read time.

**Optimistic-POST racing a reconcile read (reconcile-clobber).** `inFlightCidsRef`
**stays** — it still protects a locally-added optimistic row whose POST hasn't committed
from being dropped by a `hydrate` that reads server state mid-flight. This race is
orthogonal to the ts-swap and remains correct; only the *identity-matching* parts of
hydrate (the content heuristic) are deleted, not the in-flight preservation.

**`ts` still needed for ordering/insert.** `insertMessageBatchByTs`,
`appendMessageBatch`'s `seenTs` dedup, and the drawer `activity_at` all keep using `ts`
for **ordering and de-dup of transcript batches** — legitimate uses of ts as
display/ordering metadata. Only ts's role as *identity* is removed. Keep `_ensure_unique_ts`
(demoted) so display ts stays monotonic and these ordering paths are unaffected.

---

## 6. Sizing

**Net simplification — substantially more deleted than added.**

| Area | Added | Deleted |
|---|---|---|
| `usePendingQueue.js` | ~15 (cid-keyed confirm/promote/hydrate) | ~170 (consumedServerTsRef guard, reissue branches, content-identity hydrate heuristic, `_queueIdentityKey`) |
| `ChatView.jsx` | ~20 (pinSentMessage funnel + cid mint) | ~180 (7 hand-rolled pin blocks → 7 one-liners, 2 retarget branches deleted, ts-keyed pin-intent maps, optimistic-ts pin comment/logic) |
| `chatRuntimeState.js` | ~2 | ~40 (`resolveFreshPinRetarget`, `resolveSteeredPinDecision`) |
| `useScrollMode.js` | ~4 (`_lastUserRowEl` split) | ~15 (last-row fallback, ts→cid renames net-zero) |
| `QueuedMessages.jsx` | 0 | ~3 (fallback) |
| Backend (`chats_stream`, `chat_writer`, runners, schemas) | ~40 (cid plumbing, echoes, cid dedup, idempotent-POST) | ~40 (content byte-match, `(ts,content)` dedup complexity, ts-bump identity comments) |
| **Total** | **~80 LOC** | **~450 LOC** |

**Rough net: −370 LOC**, and — more important than line count — **four whole
compensation mechanisms are removed outright** (the deferred-removal cid-guard, the
hydrate content-identity heuristic, the pin last-row fallback + optimistic-ts retarget,
and the `(ts,content)` steer dedup), each of which was itself a source of the bugs this
redesign closes. The added code is one funnel and one field on the wire. Complexity
drops far more than the LOC delta suggests because the deleted code is the densest,
most-commented, most-race-prone in the subsystem.
