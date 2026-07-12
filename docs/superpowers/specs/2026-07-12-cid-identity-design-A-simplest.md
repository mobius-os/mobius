
# Chat message identity — fix-forward redesign (`cid` everywhere)

Read-only design against `origin/main` tip `8ee77cc2` in worktree
`/home/hmzmrzx/projects/mobius/.claude/worktrees/chat-identity`. Nothing was edited.

## 0. The one-line thesis

A user message already has a client-minted identity — `cid` — but it stops at the network
boundary. Half the compensation machinery in ChatView / usePendingQueue / chat_writer exists
solely because the *other half* of the message's life (the wire, the DB, the DOM pin, the queue
DELETE, the steer dedup) is keyed on `ts`, which is not stable (optimistic `Date.now()` → server
canonical → `_ensure_unique_ts` +1ms bump). **Push the existing `cid` across the wire, persist it,
echo it, key the DOM/pin/queue/steer on it, and demote `ts` to pure display/ordering metadata.**
Every ts-identity compensation then has nothing left to compensate for and is deleted.

The design is deliberately the *smallest* one that fits: it reuses the `cid` field, the
`queuedPinIntentByCidRef` map, the `inFlightCidsRef` set, the `PIN_USER_MSG` mode, and the
`settleNonPinMode`/`makeSendPinIntent` helpers that already exist. It adds one wire field, one DOM
attribute, one funnel function, and one `cidOf()` read-helper. It deletes far more than it adds.

---

## 1. Identity lifecycle

### 1.1 Where `cid` is minted

**Once, at compose time, for BOTH send paths.** Today only the queue branch of `doSend`
(`ChatView.jsx:1571`) mints a `cid`; the fresh-send branch (`:1809`) builds
`{role, content, ts, optimistic:true}` with no cid. Unify: mint at the top of `doSend`, before the
queue-vs-fresh branch, and stamp it on whichever optimistic row is created.

```js
const cid = (crypto?.randomUUID?.())
  ?? `cid-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
```

- Fresh-send optimistic row: `{ role:'user', content:text, ts:Date.now(), cid, optimistic:true }`.
- Queue optimistic row: `{ role:'user', content:text, ts:Date.now(), cid, queued:true }` (as today,
  minus the locally-generated cid which now comes from the shared mint).
- `doSendSilent` (hidden answer) mints a cid too for symmetry, but it is inert — hidden rows never
  render a user bubble or pin. Keep it so the persisted row still has an identity for reload dedup.

### 1.2 Wire format

Field name: **`cid`** (string). It rides the existing shapes; no new endpoints.

| Direction | Carrier | Change |
|---|---|---|
| Client → server | `POST /messages` body | `schemas.SendMessage.cid: str \| None = None` |
| Server → client (fresh start) | `202 {status:"started", message:{…}}` | `message` already is `_user_message_from_body(...)`; it now carries `cid` |
| Server → client (queued) | `202 {status:"queued", pending_message:{…}}` | `pending_message` carries `cid` |
| Server → client (started-from-queued) | `202 {…, message:{_messages:[…]}}` | each row in `_messages` carries `cid`; `_consumed_ts` → **`_consumed_cids`** |
| SSE | `queued_turn_starting` `{message, message._messages, message._consumed_cids}` | rows carry `cid`, consume list is cids |
| SSE | `steered_into_turn` `{messages:[{cid,…}]}` | each steered row carries `cid` |

`consume_pending_ts` on the request (`SendMessage`) becomes **`consume_pending_cids: list[str]`**
(force-steer selects queued rows by cid). `steered_messages` hint rows carry `cid`.

### 1.3 Where it is persisted

`cid` becomes a first-class field of the message dict in **both** `Chat.messages` and
`Chat.pending_messages` (the SQLite JSON columns). It is written by `_user_message_from_body`
(copy `body.cid`), preserved by every actor command that copies user rows
(`StartTurn._append`, `_append_pending`, `_append_steered_user_message`,
`_pending_messages_for_transcript`, `_user_messages_from_pending`,
`_combine_pending_messages`), and **not stripped** on the way to the client. The one required
polarity flip: `stripInternalUserMessageFields` (`chatRuntimeState.js:6`) currently strips `cid`
(it was frontend-only); it must now **keep** `cid` and strip only `queued/position/_consumed_*/
_messages/_agent_content/_initiated_by_app_id`.

Message shape after this change (user rows):
`{ role:"user", content, ts, cid, hidden?, attachments?, timezone?, viewport? }`. Assistant/tool
rows are unchanged (they have `id`/`ts`, no `cid` — see §5 legacy rule for how the DOM handles both).

### 1.4 Lazy legacy derivation

No migration. A persisted row without `cid` (pre-change transcript, or an assistant row) gets a
derived identity **at read time** via one pure helper used everywhere identity is consumed:

```js
export function cidOf(msg) {
  return msg?.cid ?? (msg?.ts != null ? `legacy-${msg.ts}` : null)
}
```

`legacy-<ts>` is stable for a given persisted row (ts is immutable once persisted), so a legacy
user message pins and keys correctly for the rest of its life. Assistant rows keep using
`msg.id || role-ts` for `data-key` (ANCHOR_AT) exactly as today — `cid`/`cidOf` is only for the
user-row `data-cid` used by pins and the queue.

---

## 2. Exact surface changes — file by file

Legend: **ADD** / **CHANGE** / **DELETE**. The DELETE list is the payoff.

### 2.1 `backend/app/schemas.py`
- **CHANGE** `SendMessage`: add `cid: str | None = None`; rename `consume_pending_ts` →
  `consume_pending_cids: list[str] | None`. (`steered_messages` hint rows already `list[dict]`;
  they now carry `cid` — no schema change, it's `dict`.)

### 2.2 `backend/app/routes/chats_stream.py`
- **CHANGE** `_user_message_from_body`: copy `body.cid` onto the dict.
- **CHANGE** `_selected_force_steer_pending` / `_force_steer_matches_pending`: select by
  `body.consume_pending_cids` against `m.get("cid")` (falling back to `cidOf`-equivalent
  `legacy-<ts>` for legacy queue rows), instead of `consume_pending_ts` against `m["ts"]`.
- **CHANGE** `_user_messages_from_pending`: keep `cid` (still strip `queued/serverTs/position`).
- **CHANGE** the `steered_into_turn` broadcast payload builder (`:780`): include `cid` on each row.
- **CHANGE** `_steer_into_active_turn` / `_split_steer_at_route` / `AppendSteeredUserMessage`
  submit: pass `consume_pending_cids` through where `consume_pending_ts` went.
- **CHANGE** the started/queued responses: they already echo the row dict; `cid` now flows for free.

### 2.3 `backend/app/routes/chats_stream.py` — DELETE `/pending/{ts}` route identity
- **CHANGE** `@router.delete("/{chat_id}/pending/{ts}")` → `.../pending/{cid}` (path param `cid: str`),
  `CancelPending(cid=…)`.

### 2.4 `backend/app/chat_writer.py`
- **CHANGE** `CancelPending`: field `ts: int` → `cid: str`; `_append_pending`'s cancel path and the
  cancel command filter on `m.get("cid") == cid` (legacy fallback `legacy-<ts>`).
- **CHANGE** `_combine_pending_messages`: `_consumed_ts` → `_consumed_cids` (list of `cidOf` per
  pending row).
- **CHANGE** `_append_steered_user_message`: dedup key `_steer_dedup_key = (ts, content)` →
  **`cid`** (`seen_keys = {m["cid"] for user rows}`; drop a raw row whose cid is already present).
  This is a *stronger, simpler* idempotency: a re-delivered queued row has the same cid, a genuinely
  distinct send has a distinct cid — no content compare, no ts-tuple.
- **CHANGE** `_pending_messages_for_transcript`, `_append_pending`, `StartTurn._append`: keep `cid`.
- **DELETE** `_ensure_unique_ts` (`:2004`) **and all four call sites** (`_append_pending:1396`,
  `_append_steered_user_message:1484`, `_pending_messages_for_transcript:2090`,
  `_apply_last_assistant_message:2226/2233`). Its two stated jobs — "unique React key" and
  "unambiguous DELETE-by-ts" — are both now served by `cid`. `ts` is assigned once by the server
  (`int(time.time()*1000)`) as ordering/display metadata and never bumped. Equal ts across
  sibling rows is now harmless (keys are `cid`/`id`; render order is array order). *(This is the
  "ts-bump identity role" death named in the mandate — it also removes the disguised-duplicate
  vector from yesterday's steered-twice bug at its root.)*

### 2.5 `backend/app/chat.py`
- **CHANGE** `queued_turn_starting` publish (`:2872`): `message` already carries `_consumed_cids`
  (from `_combine_pending_messages`) and `_messages` rows carry `cid`.
- **CHANGE** any `_consumed_ts` reader in the promote path → `_consumed_cids`.

### 2.6 `backend/app/claude_sdk_runner.py` + `codex_sdk_runner.py`
- **CHANGE** `ActiveClaudeClient.steer` buffer dedup (`:266-280`): `_steer_user_msgs` dedup keyed on
  **`cid`** (`buffered = {m["cid"]}`) instead of `ts`; `_steer_consume_ts` → `_steer_consume_cids`.
  Codex peer path likewise.

### 2.7 `frontend/src/components/ChatView/chatRuntimeState.js`
- **CHANGE** `stripInternalUserMessageFields`: **keep** `cid` (remove it from the destructure-strip).
- **ADD** `cidOf(msg)` (§1.4).
- **CHANGE** `continuationRowsFromPromotedMessage` / `startedMessagesFromResponse`: unchanged logic,
  now cid survives through them.
- **CHANGE** `serverSnapshotBehindLocal`: match on `cidOf` set instead of `ts` set (cleaner —
  optimistic rows are detected by cid absence, not the `optimistic/queued/serverTs===false` flag
  triad). Optional; the flag triad still works, but cid is the natural key.
- **DELETE** `resolveFreshPinRetarget` (`:103`) — the entire "re-point the pin at the canonical
  server ts" helper. With a stable cid the row identity never changes across optimistic→server, so
  there is nothing to retarget.
- **DELETE** `resolveSteeredPinDecision` (`:86`) — folded into the pin funnel (§3); the intent-
  staleness check it wraps moves into the funnel.

### 2.8 `frontend/src/components/ChatView/useScrollMode.js`
- **CHANGE** ScrollMode union: `PIN_USER_MSG{ts}` → **`PIN_USER_MSG{cid}`** (docstrings, the
  `@returns` union, the module header).
- **CHANGE** `_pinnedUserEl(scrollEl, cid)`: query `.chat__msg--user[data-cid="${CSS.escape(cid)}"]`.
  **DELETE the last-row fallback limb** (`:120-121` and the 20-line comment `:100-113`) for the
  cid-supplied case — an exact cid match always resolves the just-sent row. Keep the `cid == null`
  branch returning the last row (that is the *ref-transiently-null* helper used by `sizeSpacer`,
  orthogonal to identity).
- **CHANGE** `applyMode` PIN case, `_pinReapplyNeeded`, `sizeSpacer`'s fallback call, `settlePinnedMode`:
  pass `mode.cid` where `mode.ts` went.
- **CHANGE** `_validateSavedMode` PIN branch (`:173`): compare `cidOf(lastUserMsg) === saved.cid`.
  Legacy `{ts}` saves (no `cid`) degrade to `FOLLOW_BOTTOM` (sessionStorage is per-session, so
  legacy saves evaporate within a session — no data at risk; §3.3).
- **ADD** an exposed action `pinSend(...)` (§3) OR keep the funnel in ChatView — see §3 decision.

### 2.9 `frontend/src/components/ChatView/ChatView.jsx` — the big consolidation
- **CHANGE** mint cid once at top of `doSend`; stamp on both optimistic rows and pass `cid` in every
  `streamSend`/POST body.
- **DELETE** `queuedPinIntentByTsRef`, `rememberQueuedPinIntentTs`, and the ts-keyed limbs of
  `takeQueuedPinIntent` / `forgetQueuedPinIntent` (`:404, :665-698`). Keep the single
  **`queuedPinIntentByCidRef`** (`Map<cid, intent>`) — the pin intent still must survive from submit
  until an SSE event (`queued_turn_starting` / `steered_into_turn`) fires, and cid is now the stable
  lookup key present on those events' rows. `rememberQueuedPinIntent(cid, intent)` /
  `forgetQueuedPinIntent({cid})` / `takeQueuedPinIntent({cid})` collapse to cid-only.
- **CHANGE** `findOptimisticUserIndex(messages, ts)` → `findOptimisticUserIndex(messages, cid)`
  (match `cidOf(m) === cid`); `replaceOptimisticWithBatch(prev, cid, rows)` likewise.
- **CHANGE** `appendMessageBatch`/`insertMessageBatchByTs` dedup: dedup user rows by `cidOf`, keep ts
  for insert *ordering* (ts stays legit ordering metadata). Assistant rows keep ts-dedup.
- **DELETE the seven hand-rolled pin blocks** and replace each with one `pinSend(...)` call (§3.1).
  This removes, at each of the seven sites, the repeated
  `setSpacerActive(true) + if(willPin){modeRef.current={kind:'PIN_USER_MSG',...}} else {settleNonPinMode(...)}`
  plus its "do NOT zero the spacer" comment paragraph.
- **DELETE the fresh-send "pin to optimistic ts because the row never reconciles" branch**
  (`:1941-1964`) and its ~22-line comment — the whole optimistic-vs-canonical ts divergence it
  documents cannot occur once the server echoes the client's cid on the started row. The fresh-send
  and started-from-queued paths now call the identical `pinSend(cid, intent)`.
- **CHANGE** `handleCancelPending(ts)` → `handleCancelPending(cid)`; `DELETE .../pending/${cid}`;
  `QueuedMessages onCancel` passes `msg.cid`.
- **CHANGE** `handleStop` snapshot/resend: `queuedSnapshot` is unchanged; the combined resend is a
  fresh `doSend` that mints its own new cid (§5 Stop-resend). The `clearedPendingTs` reconciliation
  becomes `clearedPendingCids` (the backend reports which **cids** it cleared;
  `resolveStopResend.js` keys on cid).
- **CHANGE** `handleSteer`: build `consumePendingCids = confirmedSnapshot.map(cidOf)`; the byte-match
  content join is unchanged; pin intent stamped by cid.
- **CHANGE** render (`:3037`): add `data-cid={msg.role==='user' ? cidOf(msg) : undefined}`. Keep
  `data-ts` for the timestamp tooltip/`<time>` display and the click-to-reveal — display only.
  React `key` stays `msg.id || msg.ts || role-i`; can optionally become `cidOf(msg) || msg.id`.

### 2.10 `frontend/src/components/ChatView/hooks/usePendingQueue.js`
- **CHANGE** `_fromServerList`: `cid: m.cid ?? \`legacy-${m.ts}\`` (was `s-${m.ts}`).
- **CHANGE** `add(msg, { inFlight })`: take an **explicit `inFlight` flag** from the caller instead
  of inferring server-origin from an `s-` cid prefix. Callers: optimistic queue → `inFlight:true`;
  fresh-send-becomes-queued → `inFlight:false`. **DELETE** the `isServerOrigin = cid.startsWith('s-')`
  inference (`:188`).
- **CHANGE** `swapOptimisticTs(cid, serverTs, position, serverMsg)` → rename **`confirmServerRow(cid,
  {ts, position, serverMsg})`**: find by cid, set `ts` (display), `serverTs:true`, `position`,
  splice in `serverMsg` fields, clear the in-flight mark. **No cid-preservation dance** (cid never
  changed), **no dup-collapse branch** (`:232-238`, a hydrate can no longer insert an `s-<ts>` twin
  — hydrate matches by cid), **no guard consult**.
- **DELETE** `consumedServerTsRef` + `resolveCidFromGuard` + the entire deferred-removal guard
  (`:111-133, :160-175, :218-224` in swap, `:326-345` arm-in-`promoteManyByTs`, guard-prune in every
  promote/cancel). Its whole reason for existing — a server-**reissued ts** matching a fresh
  message's optimistic entry (bug #4) — is a *ts-identity* hazard that vanishes when promotion/removal
  key on cid. `promoteManyByCids(cidList)` removes rows by cid; a reissued ts is irrelevant.
- **CHANGE** `promoteManyByTs(tsList)` → `promoteManyByCids(cidList)`; `promoteAll(ts)` /
  `promoteByTs(ts)` → by cid (the SSE `queued_turn_starting` now carries `_consumed_cids`). Content-
  join, attachment-dedup unchanged.
- **DELETE** the content-identity reconciliation in `hydrate` (`:448-476`): `_queueIdentityKey`,
  `_attachmentKey`, `serverByIdentity`, `localInFlightByIdentity`, `cidByServerTs`,
  `matchedInFlightCids`. It exists purely to map an optimistic local row onto a server row when the
  POST ack raced the hydrate and the two rows had *different ts and no shared identity*. With cid on
  server pending rows, hydrate matches by cid directly:

  ```js
  const hydrate = (serverList, { preserveMissing } = {}) => {
    const serverCids = new Set(serverList.map(m => m.cid ?? `legacy-${m.ts}`))
    const reconciled = serverList.map(m => ({ ...m, cid: m.cid ?? `legacy-${m.ts}`, queued:true, serverTs:true }))
    const preserved = pendingMessagesRef.current.filter(m =>
      m.cid && inFlightCidsRef.current.has(m.cid) && !serverCids.has(m.cid))
    // + preserveMissing: local rows absent from server, downgraded serverTs:false (unchanged)
    apply([...reconciled, ...preserved, ...preservedMissing])
  }
  ```
- **KEEP** `inFlightCidsRef` (POST-not-yet-acked ≠ identity; still needed so a reconcile that lands
  before the ack doesn't drop a not-yet-seen row), `cancelByCid`, `clear`, `markInFlight`,
  `clearInFlight`. **DELETE** `cancelByTs` (all cancels are cid now).

---

## 3. The pin funnel

### 3.1 API

One function every send/steer/promote site calls. It owns spacer arming, intent-staleness, and mode
assignment — the three things the seven sites currently hand-roll.

```js
// intent captured at SUBMIT time by makeSendPinIntent (unchanged):
//   { willPin: boolean, submitScrollVersion: userScrollIntentVersionRef.current }
function pinSend(cid, intent) {
  setSpacerActive(true)                       // reservation is armed on EVERY visible send
  if (!intent || intent.submitScrollVersion !== userScrollIntentVersionRef.current) {
    // a real user scroll landed after submit → the user wins; do not move them,
    // but leave any stale PIN retired to their current anchor.
    settleNonPinMode(modeRef, scrollRef.current, { retireFollow: true })
    return
  }
  if (intent.willPin && cid) {
    modeRef.current = { kind: 'PIN_USER_MSG', cid }   // spacer height stays owned by sizeSpacer
  } else {
    settleNonPinMode(modeRef, scrollRef.current, { retireFollow: true })
  }
}
```

- **Owns spacer arming** — `setSpacerActive(true)` lives here, once, instead of at seven sites.
- **Owns intent-staleness** — the `userScrollIntentVersionRef` comparison (the "a user scroll after
  submit must still win" contract) is enforced in exactly one place. `makeSendPinIntent`
  (`ChatView.jsx:648`) is renamed to stamp `submitScrollVersion` and stays. `pinIntentStillCurrent`
  is deleted (its check is inlined here).
- **Owns mode assignment** — the only writer of `PIN_USER_MSG` on the send path. It never zeroes the
  spacer (that invariant, spelled out in seven duplicated comments today, becomes one comment here).

### 3.2 How the seven sites reduce

| Site (current line) | Before | After |
|---|---|---|
| fresh send (`:1831`) | pin-block A | `pinSend(cid, freshPinIntent)` |
| fresh→started retarget (`:1934-1968`) | pin-block + `resolveFreshPinRetarget` + commit | `pinSend(cid, freshPinIntent)` then commit **(retarget DELETED)** |
| fresh→queued→started (`:1893-1921`) | pin-block + retarget | `pinSend(cid, freshPinIntent)` **(retarget DELETED)** |
| queue→started (`:1664-1674`) | pin-block | `pinSend(cid, queuedPinIntent)` |
| queue→started race (`:1725-1735`) | pin-block | `pinSend(cid, queuedPinIntent)` |
| continuation (`onStreamEnd continues`, `:880-889`) | pin-block + fallbackWillPin | `pinSend(pinCid, continuationPinIntent)` |
| steer (`onSteeredIntoTurn`, `:1008-1016`) | pin-block + `resolveSteeredPinDecision` | `pinSend(pinCid, steerPinIntent)` **(resolver DELETED)** |

`pinCid` in the SSE handlers is `cidOf(promotedRows[0])` / `cidOf(steeredMessages[0])` — the cid the
backend echoed, looked up against `queuedPinIntentByCidRef`. **The two retarget branches vanish
entirely** because "pin optimistic ts, then re-point to server ts" collapses to "pin cid" — the cid
is identical on the optimistic row and the server row.

### 3.3 Saved-mode persistence migration

- `persistMode` writes `{kind:'PIN_USER_MSG', cid}` (was `{…, ts}`).
- `_validateSavedMode` PIN branch: `return cidOf(lastUserMsg) === saved.cid ? saved : {kind:'FOLLOW_BOTTOM'}`.
- **Legacy `{ts}` fallback:** a saved mode with `ts` and no `cid` degrades to `FOLLOW_BOTTOM`.
  Justification: `chat-mode` lives in `sessionStorage` (per tab session), so a legacy save can only
  exist for the lifetime of one already-open tab spanning the deploy; the cost of the fallback is
  one chat opening at the bottom instead of restoring a pin — negligible, and self-heals on the next
  send. No need to try `legacy-<ts>` matching here (the DOM row would need a matching `legacy-<ts>`
  cid, which only exists if the row itself is legacy — true, so `saved.ts && cidOf(last)===
  'legacy-'+saved.ts` *would* work; include it as a one-line best-effort if cheap, else degrade).

### 3.4 Decision: funnel lives in ChatView

`spacerActive` and `setSpacerActive` are ChatView state; `modeRef`, `scrollRef`,
`userScrollIntentVersionRef` are already in ChatView scope (returned by `useScrollMode`). A ChatView-
local `pinSend` closes over all of them with zero new plumbing and matches the repo-native pattern
(`makeSendPinIntent`, `settleNonPinMode` are already ChatView/​module-local). Exposing it as a
`useScrollMode` action would force `spacerActive` down into the hook — more surface, not less.
**Keep it in ChatView.**

---

## 4. Contracts that must keep holding + test disposition

### 4.1 Contracts (unchanged behavior, new mechanism)
- **CLAUDE.md "Chat UX #1" send rule** — fresh sends always pin; mid-turn-scrolled-up sends don't
  move. Enforced by `pinSend` (funnel) reading the same submit-time `willPin` + scroll-version. ✅
- **"Chat UX #2/#3" no-fight / re-anchor-on-promote** — `PIN_USER_MSG` re-pin logic in
  `useScrollMode` is byte-for-byte the same, only keyed on cid. ✅
- **Stop-chat contract** (`docs/stop-chat-contract.md`) — interrupt + resend-as-one-fresh-turn.
  Unchanged; resend mints a new cid (§5). ✅
- **Message queue contract** (CLAUDE.md "Message queue") — the "unique timestamps within the queue"
  clause is **retired** (its rationale, duplicate React keys + ambiguous DELETE-by-ts, is now served
  by cid). Update that doc paragraph to say identity = cid, ts = ordering. ✅
- **`QueuedMessages.jsx` keyOf** — already `msg.cid ?? t-<ts>`; becomes `cidOf(msg)`. ✅
- **`chatContract.js`** — the executable geometry contract is **cid-agnostic** (it reasons over
  scrollTop/offsetTop geometry, never ts or cid). No change needed; only its consumers (the specs)
  select the pinned row, and they can select `.chat__msg--user` (unchanged) or `[data-cid]`.

### 4.2 Tests — change vs die vs stay

| Test | Disposition | Why |
|---|---|---|
| `tests/spacer.spec.mjs` (incl. #26 fresh-always-pins) | **STAY** | asserts pin geometry; selects the user row, not its ts. Row still pins. |
| `tests/second-send-pin.spec.mjs` | **CHANGE** | its premise is the optimistic→server ts-swap retarget; the swap is gone. Keep the end-state assertion (2nd send lands flush at top); drop assertions that inspect a ts change. |
| `tests/pin-clamp-settle.spec.mjs` | **STAY** | clamp-settle is geometry; cid-keyed pin re-pins identically. |
| `tests/steer-queued.spec.mjs` | **CHANGE** | steer consume list ts→cid; steered row identity assertions ts→cid. |
| `frontend/.../__tests__/chatRuntimeState.test.js` | **PARTIAL DIE** | `resolveFreshPinRetarget` / `resolveSteeredPinDecision` tests **die** (functions deleted); add `cidOf` + `stripInternalUserMessageFields`-keeps-cid tests. |
| `frontend/.../__tests__/useScrollMode.test.js` | **CHANGE** | `PIN_USER_MSG{ts}`→`{cid}`, `_pinnedUserEl` by data-cid, `_validateSavedMode` cid; **DELETE** the last-row-fallback test (fallback removed). |
| `frontend/.../hooks/__tests__/usePendingQueue.test.js` | **HEAVY DIE + CHANGE** | all `consumedServerTsRef`/reissued-ts guard tests **die**; hydrate content-identity-match tests **die**; `swapOptimisticTs`→`confirmServerRow` test **changes**; `promoteManyByTs`→`promoteManyByCids` change; `_fromServerList` `s-`→`legacy-`/`cid` change. |
| `frontend/.../__tests__/optimisticSteerQueue.test.js` | **CHANGE** | consume ts→cid; restore-on-not-steered keyed on cid. |
| `frontend/.../__tests__/chatContract.test.js` | **STAY** | geometry only. |
| `backend/tests/test_chat_message_ts.py` | **DIE (or repurpose)** | it locks `_ensure_unique_ts`; that function is deleted. Repurpose into a test asserting cid uniqueness / ts-need-not-be-unique. |
| `backend/tests/test_chat_writer.py` | **CHANGE** | `_append_steered_user_message` dedup (ts,content)→cid; `CancelPending` ts→cid; drop `_ensure_unique_ts` assertions. |
| `backend/tests/test_chats_stream_steer.py` | **CHANGE** | `consume_pending_ts`→`consume_pending_cids`; force-steer match by cid. |
| `backend/tests/test_claude_sdk_runner.py` / `test_codex_sdk_runner.py` | **CHANGE** | steer buffer dedup ts→cid. |
| `backend/tests/test_chats_stream_answer_race.py`, `test_chat_queue_module.py`, `test_chat_writer_contention.py` | **STAY** (spot-check) | actor serialization / answer-race semantics don't depend on message identity; only touch if they hard-code `_consumed_ts`. |

---

## 5. Risks & edge cases

- **Reload mid-turn / SSE catch-up.** Catch-up replays `queued_turn_starting` / `steered_into_turn`;
  those rows now carry `cid`, so `insertMessageBatchByTs` (cid-deduped) can't double-insert on
  replay — this is *stronger* than today's ts dedup (which the +1ms bump could defeat). The mounted
  DB partial (bridge) is assistant content — no cid, unaffected. ✅
- **Multi-tab.** Two tabs compose independently → two different UUIDs → no collision by construction.
  A row persisted by tab A arrives in tab B's hydrate with A's cid; B has no local optimistic twin,
  so it renders as a plain server row. Today's ts-collision risk across tabs (both `Date.now()`) is
  *removed*. ✅
- **Stop-resend path (`handleStop`).** The combined resend is semantically a **new** message (one
  combined turn from N queued rows), so it **mints a fresh cid** in its `doSend`. It must NOT reuse a
  queued row's cid — the queued rows were cleared server-side, and reusing a cid could false-match a
  lingering `queuedPinIntentByCidRef` entry or a replayed pending row. `clearedPendingCids` from the
  backend is only used to decide *whether* to resend (the PM-115 double-send guard), keyed on cid
  now instead of ts. ✅
- **Steer dedup semantics.** A repeated force-steer of the same still-live queued row delivers the
  same `cid`; runner buffer dedup (`_steer_user_msgs` by cid) and writer dedup (`seen_keys` by cid)
  both drop it. A genuinely new send has a new cid → never dropped. This is the *exact* class the
  (ts,content) tuple was approximating — cid makes it precise and removes the content compare. ✅
- **Server echo on 202 vs stream.** Both the synchronous 202 (`message`/`pending_message`) and the
  async SSE (`queued_turn_starting`/`steered_into_turn`) carry the cid the client minted, so whichever
  arrives first, the optimistic row is reconciled by cid with no race window. The old failure mode
  (ack races hydrate, two rows for one message, fast-forward rejected — `usePendingQueue:229-238`)
  is structurally impossible: both would-be rows share a cid and collapse to one. ✅
- **Legacy chats.** Pre-change user rows have no `cid`; `cidOf` derives `legacy-<ts>` at read. Since
  those rows already have unique-ish persisted ts (the old `_ensure_unique_ts` guaranteed it *within
  a chat*), `legacy-<ts>` is unique within the chat → correct pins, keys, and cancels for legacy
  rows. A legacy queued row can still be cancelled (`DELETE /pending/legacy-<ts>` → matched by
  `cidOf`) and steered (`consume_pending_cids: ['legacy-<ts>']`). ✅
- **`data-key` (ANCHOR_AT) unchanged.** Pins move to `data-cid`; ANCHOR_AT keeps `data-key`
  (`id || role-ts`). They are independent DOM attributes; no interference. ✅
- **Assistant rows have no cid.** Correct and intended — only user rows pin and queue. `cidOf`
  returns `legacy-<ts>` for them if ever asked, but the render only stamps `data-cid` on
  `msg.role==='user'`. ✅
- **One residual ts dependency to keep:** message **ordering** and `insertMessageBatchByTs`'s
  positional insert still use `ts`. That is legitimate (ts = ordering/display metadata, the mandate's
  explicit role for it). Dropping `_ensure_unique_ts` means two rows *can* now share a ts; the insert
  is stable (first-seen wins position), and equal-ts rows render in array order — verify
  `insertMessageBatchByTs` uses `>` (not `>=`) so an equal-ts row appends after, not before, an
  existing one (it does: `m.ts > row.ts`). ✅

---

## 6. Sizing

Rough, worktree-measured against the read files.

**Deleted (frontend):**
- `usePendingQueue.js`: deferred-removal guard (`consumedServerTsRef` + `resolveCidFromGuard` + arm/
  prune in swap/promote) ≈ **130 LOC**; hydrate content-identity matcher ≈ **35 LOC**;
  `swapOptimisticTs` dup-collapse + guard branches ≈ **25 LOC**; `s-`-prefix inference + `cancelByTs`
  ≈ **15 LOC**.
- `ChatView.jsx`: two retarget branches + fresh-send optimistic-ts comment block ≈ **70 LOC**;
  ts-keyed pin-intent map + `rememberQueuedPinIntentTs` + ts limbs of take/forget ≈ **45 LOC**;
  seven duplicated pin-blocks collapsed to funnel calls ≈ **90 LOC net**.
- `chatRuntimeState.js`: `resolveFreshPinRetarget` + `resolveSteeredPinDecision` ≈ **40 LOC**.
- `useScrollMode.js`: `_pinnedUserEl` fallback + comment ≈ **18 LOC**.

**Deleted (backend):** `_ensure_unique_ts` + 4 call sites ≈ **25 LOC**; steer (ts,content) dedup
tuple simplification ≈ **10 LOC**.

**Added:** `cidOf` + `pinSend` funnel ≈ **35 LOC**; cid on wire (schema, `_user_message_from_body`,
echoes) ≈ **15 LOC**; `consume_pending_cids` + cid-keyed cancel/steer plumbing ≈ **25 LOC**; unified
cid mint + `data-cid` + selector swaps ≈ **25 LOC**; cid-keyed hydrate/confirm/promote ≈ **30 LOC**.

**Net: ≈ −450 LOC deleted vs ≈ +130 added → net ≈ −320 LOC**, and the *structural* win is larger
than the line count: it removes **two stateful refs and a Map-of-Sets state machine**
(`consumedServerTsRef`, the reissued-ts guard), **one content-hash reconciliation path** (hydrate
identity matcher), **two pure helper exports** (`resolveFreshPinRetarget`,
`resolveSteeredPinDecision`), **one server-side ts allocator** (`_ensure_unique_ts`), and **seven
duplicated pin/spacer blocks** — replacing them with one wire field, one DOM attribute, one read-
helper, and one funnel. Fewest moving parts, maximum deletion, reusing the cid system that already
existed and merely stopped at the network boundary.
