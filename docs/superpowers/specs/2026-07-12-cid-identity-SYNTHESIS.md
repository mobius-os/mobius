# cid identity — synthesized design (authoritative for implementation)

Owner-mandated fix-forward redesign, 2026-07-12. Two independent designs
(A-simplest, B-robust, same directory) converged on the same architecture;
this synthesis is design A's shape with design B's robustness rulings folded
in. Where the two disagreed, the adjudication below is final.

## Thesis

A user message's identity is a client-minted `cid`, minted ONCE at compose
time, carried across the wire, persisted in both `Chat.messages` and
`Chat.pending_messages`, echoed in every server payload that returns a user
row, stamped on the DOM as `data-cid`, and used as: React key for user rows,
pin target (`PIN_USER_MSG{cid}`), queue cancel key (`DELETE /pending/{cid}`),
force-steer selection (`consume_pending_cids`), and steer dedup key.
`ts` is demoted to display/ordering metadata only.

## Adjudications (where A and B differed)

1. `_ensure_unique_ts` — DEMOTE, do not delete (B wins). Transcript batch
   ordering/dedup (`insertMessageBatchByTs`, `appendMessageBatch` seenTs)
   still key on ts; keep the +1ms bump so display ts stays monotonic, and
   rewrite its docstring: it no longer serves identity (React keys and
   DELETE are cid-based now).
2. Duplicate-cid POST — ACCEPT B's idempotent-append: `AppendPending` (and
   the fresh-start path) treating an incoming cid that already exists in
   pending+messages as a duplicate POST retry → return the existing row,
   append nothing. cid is untrusted input, never an auth boundary.
3. Stop-resend — mint a FRESH cid for the combined follow-up (both agreed);
   `cleared_pending_ts` → `cleared_pending_cids` end to end.
4. `optimistic: true` marker — KEEP (A's smaller diff). Only identity
   changes; the reload-strip logic that reads it is orthogonal.
5. Funnel — ChatView-local `pinSentMessage(cid, { willPin, intent })` per
   B's sketch (owns setSpacerActive arming, intent-staleness via
   pinIntentStillCurrent, PIN-vs-settleNonPinMode write). Do NOT move
   modeRef into the hook this round.
6. Saved mode — `PIN_USER_MSG` persists `{cid}`; a legacy sessionStorage
   save carrying only `{ts}` degrades to FOLLOW_BOTTOM (ephemeral,
   self-heals).
7. Pin selector strictness — `_pinnedUserEl(scrollEl, cid)` matches
   `.chat__msg--user[data-cid="..."]` (CSS.escape) with NO last-row
   fallback; add `_lastUserRowEl(scrollEl)` for the one spacer-geometry
   caller that legitimately wants "last user row in DOM".
8. Legacy derivation — `cidOf(msg) = msg.cid ?? (msg.ts != null ?
   `legacy-${msg.ts}` : null)` applied at BOTH boundaries (frontend read
   normalizer + backend echo/selection against legacy rows) so derived
   values compare equal across the wire.

## Deletion list (the point of the exercise — all must actually be deleted)

- `chatRuntimeState.js`: `resolveFreshPinRetarget`, `resolveSteeredPinDecision`
  (folded into the funnel); `stripInternalUserMessageFields` KEEPS cid and
  strips `_consumed_cids` alongside the other `_`-fields.
- `ChatView.jsx`: both fresh-send retarget branches; the seven hand-rolled
  pin blocks (each becomes one `pinSentMessage` call); the ts-keyed
  pin-intent plumbing (`queuedPinIntentByTsRef`, `rememberQueuedPinIntentTs`,
  ts/tsList params of `forgetQueuedPinIntent`/`takeQueuedPinIntent`);
  `findOptimisticUserIndex`→`findUserIndexByCid`; `replaceOptimisticWithBatch`
  filters by cid.
- `useScrollMode.js`: the `_pinnedUserEl` last-row identity fallback + its
  comment; `PIN_USER_MSG{ts}` → `{cid}` everywhere (applyMode,
  `_pinReapplyNeeded`, `settlePinnedMode`, `_validateSavedMode`, docs).
- `usePendingQueue.js`: the `consumedServerTsRef` deferred-removal guard and
  `resolveCidFromGuard`; `swapOptimisticTs`'s reissue/twin branches (becomes
  `confirmQueued(cid, {ts, position, serverMsg})` — updates display fields on
  the cid-matched row, identity never changes); the hydrate content-identity
  heuristic (`_queueIdentityKey`, `serverByIdentity`, `localInFlightByIdentity`,
  `cidByServerTs`, `matchedInFlightCids`) — hydrate matches by cid;
  `promoteByTs`/`promoteManyByTs` → `promoteByCid`/`promoteManyByCid`;
  `_fromServerList` cid fallback `legacy-<ts>`. KEEP `inFlightCidsRef` (guards
  the optimistic-POST-vs-reconcile race; orthogonal and correct).
- `QueuedMessages.jsx`: `keyOf` → `msg.cid` unconditionally; `onCancel(msg.cid)`.
- `chats_stream.py`: `_selected_force_steer_pending` selects by
  `consume_pending_cids` and the content byte-match check DIES;
  `DELETE /pending/{ts}` → `/pending/{cid}` outright; all echoes carry cid;
  `_consumed_ts` → `_consumed_cids` in queued_turn_starting payloads.
- `chat_writer.py`: `CancelPending` by cid; `_append_steered_user_message`
  dedup by cid (replace `_steer_dedup_key` (ts,content)); every dict-copy
  path preserves cid (`_pending_messages_for_transcript` must not pop it);
  `_ensure_unique_ts` demoted per adjudication 1.
- runners (`claude_sdk_runner.py`, `codex_sdk_runner.py`): steer buffer dedup
  by cid; `consume_pending_ts` → `consume_pending_cids` on handles.
- `schemas.py`: `SendMessage.cid`; `consume_pending_cids` replaces
  `consume_pending_ts` (removed outright — no non-browser caller exists).
- Message row renderer: user rows get `data-cid={cidOf(msg)}` and React
  key `cidOf(msg)`; `data-ts` stays for the timestamp tooltip only.
- `chatContract.js`: geometry predicates unchanged; only harness/selector
  docs that reference data-ts for locating the pinned row switch to data-cid.

## Contracts that must keep holding

All nine Chat-UX constraints in AGENTS.md (fresh-send-always-pins,
reserve-on-send + idle-mount no-spacer, mid-turn no-yank, re-anchor on
promote, fullViewH, hide-then-reveal, leave-freezes-position, no-jitter,
single assistant surface), the queue lock + single-writer actor guardrails,
and the Stop-chat contract (resend narrowed to the cleared cid-set).

## Test routing

Rename-only: useScrollMode.test data-ts cases → data-cid; spacer/second-send-
pin/pin-clamp-settle/send-rule spec locators → data-cid; steer specs and
writer tests → cid keys; Stop tests → cleared_pending_cids.
Die: the last-row-fallback unit test; retarget helper tests; the
consumedServerTsRef / swapOptimisticTs twin-collapse tests; the byte-match
steer route test.
New: cid stable across optimistic→confirm (no remount); hydrate matches by
cid preserving local UI state; legacy `legacy-<ts>` derivation equality
across wire; duplicate-cid POST idempotency; Stop-resend mints fresh cid;
steer re-delivery dropped by cid while identical-text distinct sends both
persist.
