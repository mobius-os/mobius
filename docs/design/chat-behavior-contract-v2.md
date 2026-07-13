# Chat behavior contract v2 — mode-based scroll, stable returns, lazy tool output

Owner-directed (2026-07-12). Fix-forward; no backwards compatibility. This
supersedes the pin/spacer behavior encoded in the current lock-in specs
(send-rule, second-send-pin, and parts of spacer) — those specs get REWRITTEN
to this contract, deliberately, as part of item 1. It builds on the
simplification design pass (mechanism maps, two design arms, adversarial
critiques — session artifacts 2026-07-12) whose root-cause finding stands:
`modeRef` transitions are invisible to React, so every send site hand-disciplines
reconciliation; six of the seven recent scroll bugs trace there.

## The contract (owner's rules, verbatim intent)

1. **Reservation is unconditional.** Every new user message — fresh send or
   steer — reserves exactly enough scroll room that scrolling to the bottom
   puts the message at the top of the viewport with the space below it free
   for the response. Exception: a message taller than the viewport reserves
   nothing (there is no "top" framing to protect; pinning still targets the
   message start).
2. **Two modes per chat: AUTO (auto-scroll) and STAY (stay where you are).**
   New chats are STAY — including after the first message. Mode is explicit
   state, not a per-send reset.
3. **Manually scrolling to the bottom engages AUTO.** Scrolling away from the
   bottom returns to STAY (implied by the mode being two-state; the gesture
   window discipline from the current engine stays).
4. **A send or steer moves the viewport ONLY in AUTO mode — plus the first
   message of a chat.** The message goes to the top with just enough spacer
   for the response to fill (rule 1's reservation). In STAY mode a send
   reserves (rule 1) but never yanks.
5. **Returning to a chat restores the exact leave position, regardless of
   mode — and leaving demotes AUTO to STAY.** Restore must not visibly move:
   no item redraws, no layout shift from tool outputs/screenshots settling,
   even when the chat is mid-stream. (Hide-then-reveal stays; the new work is
   stream-item identity across leave/return.)
6. **Tool outputs are grouped (running vs finished) and lazily hydrated.**
   The wire carries a reduced output by default; expanding a tool block
   fetches the full output on demand.

Two follow-on tracks the owner named but has not specified (idea cards, do
not build yet): the agent maintaining a running summary while it works, and
provider switching that compacts from that summary into the new provider's
context.

## Why this is a simplification, not a feature

Rule 4 removes the deepest coupling in the current engine: pin policy no
longer reads geometry. Today `shouldPinSend` consults `isNearContentBottom`
(geometry), which is how the cushion bug class (a) existed at all. Under v2
the pin decision is `mode === AUTO || isFirstMessage` — pure state. The
"geometry must never feed policy" property the design critique named as the
single most valuable structural fix falls out of the contract itself.

## Item 1 status — implemented via the cid landing (2026-07-12, adjudicated)

Both sessions independently implemented this contract. Main's landing
(0fb832ce..7f142bab + design record 6dff3e48) won the adjudication: cid
stable identity supersedes the ts+fallback pin model below, the retarget
machinery is already deleted, chatContract.js was re-justified and KEPT
(browser-injectable invariant module, .pm 210/208), and main's own
pinSentMessage funnel + userScrollIntentVersionRef cover the two
self-review bugs. The commitMode/modeVersion pure-state engine (branch
session-scroll-v2, kept on origin) is parked pending ONE open owner
ruling: short-chat second-send — main ships PIN (R2 geometric at-bottom
reading, owner-annotated in send-rule test 4); the pure-state reading of
rules 2/3 (AUTO only via manual scroll gesture) implies STAY. If the
owner rules STAY, session-scroll-v2 is the reference implementation.
The mechanics section below is retained as the design record of the
parked variant.

## Mechanics (item 1) — synthesized from the design pass

State ownership: `useScrollMode.js` owns ALL scroll state. One mutation
funnel, one writer:

- `commitMode(nextMode, {arm})` — the ONLY way ChatView changes scroll
  intent. Writes `modeRef`, arms the spacer, bumps a `modeVersion` state so
  the reconcile layout-effect deterministically runs AFTER React commits the
  DOM (kills the f#1 class: no more praying an incidental re-render fires).
- `reconcile()` — the SOLE writer of `spacerEl.style.height` and
  `scrollEl.scrollTop`. Everything (RO, scroll, keyboard, mount, commitMode)
  converges here.
- Live-gesture transitions (scroll handler engaging/retiring AUTO) write
  `modeRef` directly without bumping `modeVersion` — they react to live
  geometry and must not trigger an applyMode mid-drag.

Mode model: `STAY` replaces today's implicit anchor default; `AUTO` is
today's FOLLOW_BOTTOM. `PIN_USER_MSG{ts}` remains a transient applied state
(the enacted rule-4 move), and ANCHOR_AT remains the positional memory for
restore. The ts stays in PIN (batch-promote pins the FIRST queued row —
`promotedRows[0]` — which is not the last user row; the design critique
killed the id-less-PIN variant on exactly this).

Deletions (from the adjudicated design + enabled by the new contract):
- `PIN_BOTTOM_ROOM` — constant, formula term, contract mirror, and the
  source-grep guard that pins its text (guard updated, not orphaned).
- `shouldPinSend`'s geometry consultation + the pin-intent stamping
  machinery, to the extent the mode model obsoletes it (an intent stamped at
  submit reduces to reading the mode at commit time; verify the queued-send
  window before deleting the per-cid intent maps).
- `resolveFreshPinRetarget` / `resolveSteeredPinDecision` (optimistic→server
  ts retarget) — pin always targets the ts the rendered row actually
  carries; the last-user-row fallback in `_pinnedUserEl` stays as the net.
- `lastPinTopRef` + the three-way `_pinReapplyNeeded` enumeration → the
  closed-form reachability invariant (validate against
  pin-clamp-settle.spec — the ungated offsetTop-shift branch is a behavior
  change, per critique L3).
- `chatContract.js` runtime mirror (the Playwright specs are the contract;
  constants move to a shared `scrollConstants.js` imported by engine and
  tests — no SYNC OBLIGATION comments).

Grafts from the robust arm (per critique): one `snapshotGeometry` feeding
both the spacer formula and the at-bottom test so they cannot disagree; a
small reason-string ring buffer on scroll writes ({cause, before, after}) for
"why did it move" debugging; fold `settleNonPinMode` into the funnel
(`commitMode(anchor, {arm:false})`) so ChatView truly never writes `modeRef`.

Corrections the critique mandated: non-arming mode-set for the pagination
restore site; keyboard `preserveBottom` routes through `reconcile()` with the
pre-change `nearScrollBottomRef` mirror as an explicit input (visualViewport
fires after the move — the mirror is load-bearing, keep it); CLAUDE.md's two
stale `PIN_BOTTOM_ROOM (180px)` references get fixed to the v2 contract.

Spec rewrites (deliberate, owner-directed):
- `send-rule.spec.mjs`: near-bottom-pins → mode-based (STAY send never
  yanks; AUTO send pins; first message pins).
- `second-send-pin.spec.mjs`: premise becomes "second send pins IN AUTO
  MODE" + a new STAY twin asserting no yank + reservation present.
- `spacer.spec.mjs`: geometry cases survive (deficit-only formula
  unchanged); add taller-than-viewport no-reservation case.
- New: mode lifecycle spec (STAY default incl. after first send; engage at
  bottom; retire on scroll-away; demote on chat exit).

## Item 2 — return-without-redraw (rule 5 hard half)

Needs its own mapping before design: today a return mid-stream rebuilds
stream items from the SSE catch-up burst (reset + replay), which remounts
tool blocks and images — that is the redraw. Direction: stable item identity
(keyed by tool_use_id / block index) with catch-up RECONCILING into existing
items instead of resetting, plus dimension-stable media placeholders so
settling never shifts layout. Mapping agent dispatched; design lands as an
addendum here.

## Item 3 — grouped + lazy tool output (rule 6)

Grouping exists (groupBlocks); the new work is wire-level: events and
persisted transcripts carry a bounded excerpt (head+tail) + byte size +
truncation marker; full output stored server-side per tool_use_id; new
`GET /api/chats/{id}/tool-output/{tool_use_id}` serves the full text on
expand. Touches chat.py event shaping, chat_writer persistence,
MsgContent/ToolBlock hydration. Mapping agent dispatched; design lands as an
addendum here.

## Build order

1. Mode-based scroll core (contract rules 1–4 + exit-demotes from 5) + spec
   rewrites — the deletion-heavy engine change. Ships alone.
2. Return-without-redraw (rest of rule 5) — after its mapping.
3. Lazy tool output (rule 6) — after its mapping; backend + frontend.
4. (Separate track, unspecified) agent running summary; provider-switch
   compaction from summary.

---

I have the complete picture. All files traced, protocol confirmed, no repo files touched. Here is the deliverable.

---

# Chat Behavior Contract v2 — Addendum: Rule 5

## Rule 5 (restated)

> Returning to a chat — especially mid-stream — restores the exact leave position with **no item redraws** and **no layout shift** from tool outputs / screenshots settling.

This addendum maps *why* today's code violates it, then gives a fix direction with sizes and risk seams. All paths verified in the `design-review` worktree at `origin/main` (HEAD `ec94f5b`).

## 0. Two distinct "return" paths (they fail differently)

There is no single "return." Shell renders the chat as `<ErrorBoundary key={activeChatId}><ChatView key={activeChatId} …>` (`frontend/src/components/Shell/Shell.jsx:1696-1698`), so:

| Path | Trigger | ChatView lifecycle | Hide-then-reveal? |
|---|---|---|---|
| **A — Drawer/nav return** | leave chat → open another chat or app → come back | **full remount** (key changes → unmount + fresh mount) | yes — `revealed` starts `false` |
| **B — App foreground return** | same chat, tab hidden → visible (lock screen, app switch) | **no remount**; `visibilitychange` reconnects in place | **no** — `revealed` is already `true` |

Both funnel through the SSE **catch-up burst** (`useStreamConnection.js`), but A pays full mount cost and B re-lays-out while fully visible. Rule 5 must hold for both.

---

## 1. Remount inventory — what tears down vs. re-renders vs. stays, and why

### 1a. The catch-up reset+replay engine

On any (re)connect, `connectToStream` runs with `isCatchUp = true` and an **off-screen buffer** `catchUpItems = []` (`useStreamConnection.js:641-652`). Replayed events mutate the buffer, not the visible `streamItems`; the buffer is swapped in atomically at `catch_up_done` / `done` via `commitCatchUp()` → `setStreamItems(catchUpItems)` (`:654-670`, applied `:715-721`, `:931-933`). This already prevents the old "answer blanks during replay" bug — the visible stream survives until the commit.

**But the commit replaces the entire `streamItems` array with brand-new object identities.** React then reconciles by **key**, and every stream item is keyed by **ordinal array index**, not stable identity:
- `StreamingMessage` keys items `s-${i}` / `s-t-${i}` (`StreamingMessage.jsx:111,129,150,156,169,191,194`) where `i` is the array index.
- The whole `<li>` is **not memoized** (`StreamingMessage.jsx:97` — plain function export), so it re-renders on every commit.

Consequence: if the replay reproduces the *same count and order*, ordinal keys line up and React updates in place (component instances — incl. `ToolBlock`'s `open` state — survive). If the replay's count/order differs from what was on screen (bridge boundary moved, a tool that was mid-flight regrouped, a `text_boundary` split differently), the ordinal keys shift and React does **delete+insert = remount** of every item at and after the divergence.

### 1b. The surface swap (the dominant redraw on Path A)

On remount mid-stream the fetch effect **keeps the DB partial** (does not strip it) and connects: `commitMessages(msgs)` then `connectToStream(false)` while `data.running` (`ChatView.jsx:1328-1384`). So the assistant answer is first painted by **`MsgContent`** (the persisted-transcript renderer). When catch-up commits, `chooseActiveAssistantSurface(activePartialMsg, streamItems)` (`streamPromotion.js:215-240`, consumed `ChatView.jsx:2706-2709`) flips `hideMessage`, which suppresses the DB `<li>` (`ChatView.jsx:2994-2998`) and renders **`StreamingMessage`** instead (`:3064-3070`).

These are **two different component subtrees with independent key namespaces**:

| | Persisted surface | Live surface |
|---|---|---|
| Renderer | `MsgContent` (`MsgContent.jsx`) | `StreamingMessage` |
| Text | `StandardMarkdown` | `ProgressiveMarkdown` + trailing `<span class="chat__cursor">` (`StreamingMessage.jsx:157-158`) |
| Block keys | `i` / `t-${i}` (`MsgContent.jsx:97,151,224`) | `s-${i}` / `s-t-${i}` |
| Memoized | yes, on `msg` identity (`MsgContent.jsx:270-288`) | no |

Because the keys and even the DOM differ (`data-is-streaming`, cursor span, `ProgressiveMarkdown`'s `md-blocks` wrapper), the swap is a **full teardown of the `MsgContent` `<li>` and rebuild as the `StreamingMessage` `<li>`** — every `ToolBlock`, every `<img>`, every KaTeX/highlight node inside the answer is destroyed and recreated. This is the single biggest "returning redraws items" source, and it fires on *every* mid-stream return on Path A. (At `done`, `promoteStreamToMessages` swaps back the other way — `ChatView.jsx:1095-1155`, `836` — a second, symmetric redraw, but that one is the promote re-anchor covered by constraint #3, not Rule 5.)

### 1c. Per-component remount/state table

| Component | On catch-up commit (same surface) | On surface swap / count-mismatch | Why |
|---|---|---|---|
| `StreamingMessage` `<li>` | re-render (not memoized) | n/a (it *is* the new surface) | `StreamingMessage.jsx:97` |
| `ToolBlock` | re-render in place if ordinal key holds; **`open`/`fullOutput` state preserved** | **remount → `open` resets to `false`, cached `fullOutput` lost** | local `useState` `ToolBlock.jsx:79,87`; lazy fetch `:98-109` |
| `ExpandableImage` (`<img>`) | new object but same `src` → React keeps node | **new `<img>` element → browser reload → `onLoad` re-measure** | `InlineContent.jsx:129-189`; media-token async `useEffect:137-150` |
| `CodeBlock` | re-render; `highlightSync` reused | remount → re-highlight (sync if hljs warm, else async `setState`) | `blocks.jsx:29-65` |
| KaTeX (`MathBlock`/`InlineMathSpan`) | synchronous re-render, no settle | synchronous rebuild | `blocks.jsx:139-145`, `InlineContent.jsx:212-227` |
| `MemoBlock` (markdown block) | skipped unless `token.raw` changed | rebuilt | `blocks.jsx:174-176` |
| `MsgContent` (history above the turn) | **stable — skipped** by memo | stable | `MsgContent.jsx:270-288` |

Key point: history *above* the active turn is safe (memoized on `msg` identity). The churn is confined to the **active assistant answer**, but for a long build with many tool blocks and screenshots that is exactly the content the user is watching.

### 1d. Why identity is ordinal all the way down

There is **no stable per-item id on the wire**. `tool_start` carries only `{tool, input, output, status}` (`backend/app/events.py:243-250`; emitted `backend/app/claude_sdk_runner.py:745`). `tool_output`/`tool_end` are matched to a block by **position** — "last non-done tool" (`events.py:264-288`), mirrored on the frontend in `attachToolOutput` / `closeToolLifecycle` (`streamReducers.js:123-147,247-265`) and in text append ("last item is text" — `useStreamConnection.js:355-367`). The SDK *has* `block.tool_use_id` internally (`claude_sdk_runner.py:783`) but never puts it on the wire. So the entire stream contract is order-based; the frontend cannot key by identity today because identity isn't transmitted.

---

## 2. Layout-shift inventory — what settles late, and what hide-then-reveal covers

### 2a. Late-settling content

| Source | Settles when | Shift magnitude | Covered by hide-then-reveal? |
|---|---|---|---|
| **Screenshots / images** | `<img>.onLoad` fires after decode → measures true ratio and overwrites the reserved box (`InlineContent.jsx:163-179`) | **small** — box pre-reserves `aspect-ratio: var(--md-image-ratio, 4/3)` (`markdown.css:175-184`); delta = (actual ratio − 4/3). Landscape default keeps it small; portrait screenshots shrink width, not height | mount paint only (Path A); the *reload after a surface swap* re-incurs it, usually after reveal |
| **Media-token images** | second async hop: `mediaTokenParam()` resolves, *then* `<img>` mounts (`InlineContent.jsx:137-150`) → `resolvedSrc` is `null` until then, so the frame appears late | one extra late insert on top of the load delta | usually **not** — token fetch outlasts the 50ms quiet window |
| **highlight.js** | `highlightSync` if hljs warm; else async `highlightCode` → `setState` (`blocks.jsx:39-48`) | minimal height change (same line count) but a repaint | cold load only; warm return highlights synchronously |
| **KaTeX** | **synchronous** (`renderMathToString`, no effect) | none from timing | n/a — not a late source |
| **Tool `running`→`done`** | replay flips status; header text changes from `Running commands…` to the raw `name: input` label (`ToolBlock.jsx:149-151`) | header width/height can change → row reflow | happens during replay/commit; visible if after reveal |
| **Tool output expand** | user taps → lazy fetch full output (`ToolBlock.jsx:98-112`) | large, but **user-initiated**, not a return artifact; collapsed height is stable | out of scope for Rule 5 |

### 2b. What the hide-then-reveal cap does and doesn't do

`.chat__scroll` renders `visibility:hidden` (`ChatView.jsx:2973`) until `revealed` flips on a **50ms layout-quiet** window, capped at `REVEAL_CAP_MS = 1500` (`useScrollMode.js:37-43,681-690`). During the reveal window, `ANCHOR_AT`/`PIN` are re-applied on each ResizeObserver tick so lazy settle doesn't move the saved position (`:696-704,716-719`); after reveal they are deliberately **not** re-applied (the May-2026 mid-stream jitter guard).

What it covers: the **synchronous mount paint** on Path A — DB partial renders, images reserve their box, KaTeX/highlight settle — all under the cloak, then reveal.

What it does **not** cover:
1. **Live streaming never goes quiet**, so on a mid-stream mount it reveals at the **1500ms cap while still streaming** (`useScrollMode.js:679`) — the first frames after reveal are moving.
2. **The catch-up commit is async.** The surface swap (§1b) and its image reloads / status-flip reflows land when the burst arrives over the network — frequently *after* reveal — so they re-lay-out **visibly**.
3. **Path B has no reveal cloak at all.** `revealed` is set once per mount; a foreground return (`freezeStreamingReturn`, `ChatView.jsx:2553-2575`) only re-anchors the scroll *mode* — the catch-up commit re-lays-out with the chat fully visible.

Net: the cloak solves *mount-time* settle but not the *reconnect commit*, which is precisely the mid-stream-return case Rule 5 names.

---

## 3. Design direction

Three independent levers. They compose; ship in order of ROI.

### Lever 1 — Kill the surface swap: one renderer for the active answer *(biggest win)*

The DB-partial↔live swap (§1b) is the dominant redraw and is structural, not a timing bug. Today `chooseActiveAssistantSurface` picks *which of two subtrees* renders; instead, render the active assistant answer through **one renderer** whose input is "blocks," sourced from either the DB partial or the live stream, so switching source is a **prop/data change, not a subtree swap**.

- Make `MsgContent` (or a shared `AssistantBlocks`) the single renderer for both persisted and live blocks. `streamItemsToAssistantPayload` (`streamPromotion.js:253-265`) already maps stream items → the exact block shape `MsgContent` consumes, so the live path can feed the same renderer. Fold `ProgressiveMarkdown`'s only real differences (trailing cursor, `aria-live`) into `MsgContent` behind an `isStreaming` flag rather than a whole second component.
- Keep the block-key namespace identical across sources (see Lever 2) so source-switching reuses DOM nodes: `ToolBlock.open`, `<img>` elements, and highlighted code survive the switch.
- Effect: returning mid-stream re-renders the answer **in place** instead of tearing it down; images don't reload, tool blocks keep their expanded state, no reflow at the commit.

**Size: M/L.** Touches `MsgContent`, `StreamingMessage` (shrinks to a thin wrapper or is absorbed), `ChatView`'s surface-selection block (`:2694-2709,2994-2998,3064-3070`), and `ProgressiveMarkdown`. Behavior-preserving for history; the risk is regressing the existing single-surface invariants (constraint #9) and the bridge/steer promote path.

### Lever 2 — Stable stream-item identity (reconcile, don't reset+replace)

Give every stream item a **stable synthetic id** that survives reset+replay, and have the catch-up commit **reconcile into existing items** instead of overwriting the array.

- **Frontend-only first (cheap, no protocol change):** assign each item an `iid` = monotonic insertion ordinal when first created, and make catch-up rebuild align to existing `iid`s by position+type (the replay is deterministic and in the same order, so `catchUpItems[k]` corresponds to on-screen item `k`). On commit, merge fields into the existing objects (preserve identity) rather than `setStreamItems(freshArray)`. Key `StreamingMessage`/`MsgContent` blocks by `iid`, not array index. This removes the delete+insert on any count/order drift and lets React keep node identity through the commit.
- **Backend-later (robust, cross-layer):** emit `tool_use_id` on `tool_start` and correlate `tool_output`/`tool_end`/`tool_sources` by it in `events.py` (`:243-288`) and `streamReducers.js` (`:123-165,247-265`) instead of "last non-done." This makes identity real end-to-end and hardens the position-matching against interleaved/parallel tool calls. `tool_use_id` is already in hand at `claude_sdk_runner.py:783`; the Codex runner needs an equivalent.

**Size: S** for the frontend-only ordinal `iid` + keyed reconcile; **+M** to thread `tool_use_id` through both runners + `events.py` + reducers + persistence. Do the S first; it is independently valuable and de-risks Lever 1's "same key namespace" requirement.

### Lever 3 — Dimension-stable media + extend the reveal cloak to the commit

Two smaller, orthogonal fixes for the residual shift:
- **Media placeholder parity.** The aspect-ratio reserve (`markdown.css:175-184`) already prevents gross CLS, but (a) media-token images insert late because `resolvedSrc` is `null` until the token hop (`InlineContent.jsx:137-150`) and (b) the on-load ratio delta from the 4/3 default still nudges. Reserve the frame *before* the token resolves (render the `md-image-frame` box immediately, swap the `<img>` in when `resolvedSrc` lands) and, where the agent already knows screenshot dimensions (the viewport it was handed — `useStreamConnection.js:341-343,1150-1158`), carry width/height in the image markup so `--md-image-ratio` is exact on first paint, eliminating the on-load delta. **Size: S.**
- **Cloak the first catch-up commit.** Extend hide-then-reveal so a **reconnect** (not just mount) holds `visibility:hidden` — or, more surgically, freezes the anchor — across the first post-reconnect commit, covering Path B (which has no cloak today) and the async Path A commit that lands after the 1500ms cap. Reuse the existing 50ms-quiet/`REVEAL_CAP_MS` machinery (`useScrollMode.js:681-690`) gated on a "reconnecting" signal from `useStreamConnection` (`reconnecting`/`catchUpStartedRef` already exist). **Size: S/M.** Caveat: don't hide a *visible, healthy* chat on a quick Path-B flip — gate on "the socket actually reset and a catch-up burst is inbound," using the existing `QUICK_WAKE_HIDDEN_MS`/kept-socket logic so a glance at the notification shade doesn't blink the chat.

### Simplest thing that could satisfy Rule 5

If only one lever ships: **Lever 2-frontend (stable `iid` + reconcile) + Lever 1 (single renderer).** Lever 2 alone still redraws on the surface swap; Lever 1 alone still reshuffles on count drift. Together they turn "return mid-stream" into an in-place prop update of the same DOM nodes, which is the literal statement of Rule 5. Lever 3 is polish for the residual pixel-level settle and for Path B's missing cloak.

### Risk seams (adversarial checklist for whoever implements)

1. **Dedup on reconnect.** Catch-up replays the *entire* run from the start (per CLAUDE.md "Broadcast and reconnection"). Reconcile-by-`iid` must map replayed item `k` onto existing item `k` and **not** append duplicates; verify the `text_final` repair path (`useStreamConnection.js:379-389,736-745`) and `upsertQuestionItem` (`streamReducers.js:97-121`) still dedup when keys become identity-based.
2. **Ordering / interleaving.** Position-matching assumes strictly serial tool lifecycles (`events.py:1-10` states the invariant). If identity ever lets parallel tools interleave, `attachToolOutput`'s "last non-done" (`streamReducers.js:123-147`) must switch to id-match or it will attach output to the wrong block.
3. **Stop / steer interplay.** Steering seals the pre-steer segment and continues the same turn (`ChatView.jsx:948-1019`, `promoteStreamToMessages({keepTurnOpen})` `:1095-1155`); the catch-up replay of a mid-A2 steer *drops* the replayed pre-steer buffer (`useStreamConnection.js:912-923`). A reconcile that preserves identity must not resurrect the dropped pre-steer items or double-insert the steered user row (`insertMessageBatchByTs` dedups by `ts` — keep that guard).
4. **Bridge boundary.** `bridgeTs`/`findBridgeIndex` (`ChatView.jsx:1119-1126`, `hooks/useBridgePartial.js`) decides REPLACE-vs-APPEND at promote. If Lever 1 makes the live surface *be* `MsgContent` over the same `msg`, confirm the promote still replaces (not appends) the bridged row and that `streamingDataKey` (`ChatView.jsx:2824-2830`) — the scroll machine's anchor — resolves to the same node before and after the source switch, or the scroll-restore (constraint #7) regresses.
5. **Scroll-machine coupling.** `applyMode` anchors via `querySelector('.chat__msg[data-key]')` (`useScrollMode.js`, and `ANCHOR_AT` uses `data-key`); any change to which `<li>` renders the answer must keep a stable `data-key` across the return so ANCHOR_AT/PIN still resolve (this is the mechanism that "restores the exact leave position" — Lever 1 must not break it while fixing the redraw).

### Test anchors

Extend the existing suites rather than inventing new harness: `__tests__/streamSnapshotCache.test.js`, `streamPromotion.test.js`, `streamReducers.test.js`, `streamingRobustness.test.js`, `imageReserveHeight.test.js`, and the Playwright `tests/stream-reconnect.spec.mjs`. Add: (a) a reconcile test asserting object identity is preserved across a catch-up commit for unchanged items; (b) a surface-switch test asserting `ToolBlock.open` and `<img>` identity survive a mid-stream return; (c) a media-dimension test asserting the frame reserves before `resolvedSrc`.

---

**Files of record (absolute):** `/home/hmzmrzx/projects/mobius/.claude/worktrees/design-review/frontend/src/components/ChatView/{useStreamConnection.js, ChatView.jsx, StreamingMessage.jsx, MsgContent.jsx, ToolBlock.jsx, streamPromotion.js, streamReducers.js, streamSnapshotCache.js, useScrollMode.js, markdown/{BlockRenderer.jsx, blocks.jsx, InlineContent.jsx}, markdown.css}`, `.../backend/app/{events.py, claude_sdk_runner.py}`, `.../frontend/src/components/Shell/Shell.jsx`.
---

I have everything needed. The picture is clear: the lazy-fetch UX already ships, but truncation happens only at the GET serving boundary — the wire, catch-up replay, and DB row all still carry full output. The mission's design pushes truncation up to the single event funnel. Here is the deliverable.

---

# Contract rule 6 — tool outputs: grouped, reduced on the wire, full fetched lazily

Design addendum for `docs/design/chat-behavior-contract-v2.md` (the file does not yet exist; this is seed content for it). Owner-directed, fix-forward, no backwards compatibility required. All file:line refs are against `origin/main` (`5f825d3`), read-only from the `design-review` worktree.

## 0. Starting point — what already ships

Two of the three legs of this rule are already live and must not be rebuilt:

- **Grouping** exists. `groupToolRuns` folds adjacent `tool` blocks into one collapsed activity node on both the persisted and streaming render paths (`frontend/src/components/ChatView/groupBlocks.js:23`); running-vs-finished state is derived by `toolGroupState` (`groupBlocks.js:100` — `running` if any child is running, `error` on a nonzero exit, else `done`) and shown collapsed without expanding.
- **A lazy full-output fetch** exists. `ToolBlock` fetches the full output on first expand (`ToolBlock.jsx:98-112`) from `GET /api/chats/{id}/tool-output?ts=&i=` (`routes/chats.py:667-687`); the chat-load payload is trimmed by `_truncate_large_tool_outputs` (`routes/chats.py:566-602`, threshold 4096 at `:563`).

The gap this addendum closes: **that trim happens only at the `GET /api/chats/{id}` serving boundary.** The full output still streams live, still replays on every reconnect, and still lives forever in the `Chat.messages` blob. This rule moves the reduction up to the one event funnel so the wire, the catch-up burst, and the DB row all carry a bounded excerpt, with the full output stored server-side keyed by tool identity.

## 1. Current flow — event shape and size at each hop

A `tool_output` event is `{"type":"tool_output","content":<str>}`. `content` is the full result string throughout — no bound anywhere until the GET read. Bash output is plain text; a Claude Bash failure is the start-anchored `"Exit code N\n<stderr>"`; Codex/MCP results are a JSON `{stdout,stderr,exit_code}` envelope (`toolResultFormat.js:20-27`).

| Hop | Where | Shape / size | Cost of large output |
|---|---|---|---|
| 1. Runner emits | `claude_sdk_runner.py:830-834` (`_format_tool_output`); `codex_sdk_runner.py:371/386/394/402` | full string, unbounded | — |
| 2. Single funnel | `chat.py:284-359` `_ChatEventSink.publish` — the runner's `bc` IS this sink (`chat.py:181`). It calls `process_event` (persist) **and** `self.bc.publish` (wire) on the one event | full string | this is the chokepoint where wire and persist branch; truncating here fixes all downstream hops at once |
| 3. Reducer → block | `events.py:264-270` sets `blk["output"]=content` on the `{type,tool,input,output,status}` block — **no `tool_use_id` on the block** | full string in the block | block carries full output into the snapshot |
| 4. Broadcast wire + log | `broadcast.py:88-135` — pushes to live SSE subscribers **and** appends to `event_log` (cap 10 000, `:23`). **Only `text` events coalesce (`:110-124`); `tool_output` does not** | full string, once per tool | a 30-tool turn at ~150 KB each ≈ 4.5 MB streamed live that no human reads scrolling by |
| 5. Catch-up replay | `broadcast.py:137-142` `catch_up = list(event_log)` | full string ×N | every reconnect (mobile sleep/wake is frequent per `useStreamConnection`) replays the whole ~4.5 MB burst |
| 6. Persistence | `chat.py:346` `build_assistant_message` → `PersistTranscript`/`Finalize` → `chat_writer` → `Chat.messages` JSON blob | full string, forever | blob grows unbounded; `get_chat` loads the **whole** blob from SQLite into memory on every open, *then* strips (the strip is post-load — the fat read is already paid) |
| 7. GET serving | `routes/chats.py:566-602`, applied at `:647` | output > 4096 → `output:""`, `output_truncated:true`, `output_full_len` | this is the ONLY place reduced today; note it ships an **empty string**, not an excerpt |
| 8. Lazy fetch | `routes/chats.py:667-687`, keyed by `ts`+`i`, reads full from `Chat.messages` | full string | requires the fat blob to still be in the DB row |
| 9. Frontend | `ToolBlock.jsx:98-112`; wired via `MsgContent.jsx:115,227-232` (`chatId,msg.ts,blockIdx`); render capped at 20 000 chars/field (`toolResultFormat.js:37`) | excerpt-then-full | render cost already mitigated by the 20 K field cap |

**Net:** hops 4, 5, 6 (wire, replay, DB) are unaddressed; hop 7 reduces only the GET payload and reads the full back out of the fat DB row at hop 8.

## 2. Design — reduce at the funnel, stash full keyed by tool identity

### A. Bounded excerpt at the single funnel (`_ChatEventSink.publish`, `chat.py:284`)

Intercept `tool_output` before `process_event` and before `self.bc.publish`. When `len(content) > INLINE_THRESHOLD` (keep 4096):

1. Compute a bounded **head+tail** excerpt: `head` (≈2048 B) + `\n…[{full_len} B total — {shown} shown, expand for full]…\n` + `tail` (≈1024 B). Rewrite `event["content"]` to the excerpt and stamp `output_truncated:true`, `output_full_len`, `output_exit_code` (parsed int or null), and `tool_use_id` onto the event.
2. Submit a non-coalescing `StashToolOutput(chat_id, tool_use_id, full_content)` to the writer actor.

Because this is the one funnel, the excerpt now flows into the reducer (block → `Chat.messages`), the live SSE push, and the `event_log` (→ catch-up) from a single rewritten event object — live and replayed excerpts are byte-identical by construction (`broadcast.py:124-128` appends and pushes the same object).

**Failure detection is the load-bearing seam.** `formatToolResult` derives "failed" only by parsing `output` (`toolResultFormat.js:152` start-anchored `^Exit code N`; `:92` JSON envelope needs *valid* JSON). A naive mid-string cut breaks both. Two safeguards:
- **Preserve the head** so the start-anchored `Exit code N\n` survives the carve (bash case). Preserve the tail because the diagnostic bottom lines are what the reader wants.
- For a **JSON envelope**, do not carve the raw string (it stops being valid JSON and the exit code is lost). Parse it, truncate the inner `stdout`/`stderr` strings, re-serialize so the envelope + `exit_code` stay intact.
- Carry **`output_exit_code`** as an explicit block field so the frontend failure chip reads a field, never a possibly-broken re-parse.

Rejected alternative: truncate inside each of the two runners at `_format_tool_output`. It duplicates the carve across two files and still needs the sink to stash — the funnel is strictly better.

### B. Full output server-side, keyed by `tool_use_id`

**Recommended: a new SQLite table** in `ultimate.db`:

```
tool_outputs(chat_id TEXT, tool_use_id TEXT, output TEXT, created_at,
             PRIMARY KEY (chat_id, tool_use_id))
```

- `db/` is already gitignored, so these blobs are **correctly excluded from the nightly `/data` git safety-net** — we do not want megabytes of tool output versioned every night. (This is the decisive point against a naive per-chat file placed on a tracked path.)
- Written via a new **actor command `StashToolOutput`**, fire-and-forget like `PersistTranscript` (a lost stash just 404s on expand and the UI keeps showing the inline excerpt — no correctness loss). It is an **insert/upsert on a unique key**, not a read-modify-write of a shared JSON snapshot, so it is **immune to the lost-update race** the actor guardrail exists to close. Routing it through the actor keeps a single DB writer (matches "invisible robustness for the platform"); if the large blob causes FIFO head-of-line latency for the latency-sensitive question-commits, the race-free key legitimately permits a dedicated append-only connection off a thread-pool executor instead. Recommend actor-first, escalate only on measured contention.
- **Lifecycle rides the existing chat sweep.** Soft-delete keeps the rows (a recovered chat re-shows its outputs — correct). Hard purge adds one `DELETE FROM tool_outputs WHERE chat_id=?` to the tombstone-TTL purge (the reversible soft-delete pattern, features 110/113). No new TTL machinery.

**Weighed alternative: per-chat file under `/data`** (e.g. `/data/db/tool-outputs/<chat_id>/<tool_use_id>`). Precedent exists — `recovery_chat_runner.py` appends per-chat jsonl under `/data`. Pros: SQLite stays lean; blobs never touch the DB. Cons: a new filesystem cleanup path on both purge and soft-delete, atomic-write care, and it **must** sit on a gitignored path or the nightly `/data` commit versions it. Recommend the table because it inherits chat lifecycle + backup for free, and the `get_chat` memory win is already delivered simply by moving the blob out of the `Chat.messages` row.

### C. New endpoint

```
GET /api/chats/{chat_id}/tool-output/{tool_use_id}  → PlainTextResponse
```

- Auth `Depends(get_current_owner)`, mirroring the existing `get_tool_output` (`routes/chats.py:672`). Reads `tool_outputs`; 404 → frontend keeps the inline excerpt.
- **Keep the existing `?ts=&i=` endpoint (`chats.py:667`) as the legacy fallback.** Old blocks have no `tool_use_id` and no side-table row but still carry full output in `Chat.messages`, so that path keeps working unchanged.

### D. Frontend hydration on expand

`ToolBlock` already has the entire shape — `fullOutput`/`loadingFull` state, fetch-on-expand, loading text (`ToolBlock.jsx:87-112`). Changes:
- Pick the endpoint: `t.tool_use_id` present → `/tool-output/{tool_use_id}`; else legacy `?ts=&i=`.
- The inline excerpt is now **non-empty**, so an expanded block shows head+tail immediately and swaps to full when the fetch lands. A failed/offline fetch keeps the excerpt (today it shows empty — a strict offline improvement).
- Failure chip prefers `t.output_exit_code` when present; else parses the excerpt (head preserved → bash still works). `toolResultFailed`/`toolGroupState` read field-or-parse.

### E. Catch-up and persisted transcripts carry the excerpt only

Falls out of (A): the `event_log` holds the excerpt (→ catch-up), and `Chat.messages` holds the excerpt (→ `get_chat`). `_truncate_large_tool_outputs` becomes a no-op for new rows; keep it as the **legacy-row reducer** so pre-migration fat transcripts still trim on load.

### Migration (trivial — dual-read, no data migration)

Fix-forward: new turns are lean end-to-end; old fat transcripts keep working via the `?ts=&i=` path that already reads `Chat.messages`. The frontend branches on `tool_use_id`. No backfill needed. A backfill (move each large legacy `output` into `tool_outputs`, rewrite to excerpt) is possible but not worth it — the dual-read makes it optional.

## 3. Sizes, risk seams, doc touchpoints

### Sizes

| Piece | Size | Note |
|---|---|---|
| Sink excerpt carve + exit-code parse + envelope-aware inner-truncate + `StashToolOutput` submit | **M** | hot funnel; respect the `deepcopy` at `chat.py:346` and don't let the stash ride the `_steering` save-gate (`:312`) |
| Thread tool identity onto the `tool_output` event in both runners | **S–M** | Claude: `ToolResultBlock.tool_use_id` (available at `claude_sdk_runner.py:829`). Codex: the ThreadItem id in `_tool_completed_events` (`codex_sdk_runner.py:365`) — verify it carries a stable `id`; else a per-turn monotonic index works (key only needs to be stable emit→read and unique within the chat) |
| `events.py` — carry `tool_use_id`/`output_truncated`/`output_full_len`/`output_exit_code` onto the block | **S** | `:243-270` |
| `tool_outputs` table + model + `StashToolOutput` command + purge `DELETE` | **M** | new table + one actor command + one purge line |
| New `GET /tool-output/{tool_use_id}` endpoint | **S** | mirror `get_tool_output` |
| Frontend endpoint switch + excerpt-first render + field-based failure | **S** | `ToolBlock.jsx`, `groupBlocks.js` |
| Relabel `_truncate_large_tool_outputs` legacy-only | **S** | keep for old rows |
| Tests (excerpt-not-empty; stash round-trip; envelope carve keeps valid JSON; failure-survives-truncation) | **M** | update `test_chats_tool_output.py` (its `output==""` assertion at `:26` inverts to "excerpt non-empty + head preserved") |

Total ≈ one focused session (M+). Fits Ensemble+Adversarial per the risk triggers (persistence + streaming + a new table).

### Risk seams

1. **Actor bypass rules.** `StashToolOutput` is insert/upsert on `(chat_id, tool_use_id)`, not a `Chat.messages` read-modify-write, so it is outside the lost-update guardrail. Still route through the actor (single writer) or a dedicated append-only connection under measured FIFO pressure. **Never** write `Chat.messages` directly.
2. **Live-vs-replay identity / SSE.** Truncate exactly once at the sink so the same event object feeds the live push and the `event_log` append (`broadcast.py:124-128`) — excerpts are byte-identical across live and catch-up. `tool_output` isn't coalesced (`broadcast.py:110` coalesces only `text`); fine, excerpts are small and comfortably under the 10 000-entry cap.
3. **Offline.** Expand-to-load needs the network (unchanged). The inline excerpt is now a real offline fallback (today: empty). The SW does not (and should not) cache `/tool-output` — it is owner-gated dynamic; leave it uncached.
4. **Failure detection across truncation.** The one place a truncation could silently regress behavior — head-preserving carve for bash (`^Exit code N`), inner-field truncation for JSON envelopes, plus the explicit `output_exit_code` field. Cite `toolResultFormat.js:27,92,152`.
5. **Steering split.** The sink skips *transcript* saves while `_steering` (`chat.py:312`); the stash submit must **not** be gated on that, or a tool that completes during a steer split strands a truncated block with no fetchable full.
6. **Legacy rows.** Dual-read endpoint; no migration. New blocks carry `tool_use_id`; old ones fall back to `?ts=&i=` against `Chat.messages`.

### Seed / skill doc touchpoints

- **The agent's own tool-use loop is unaffected.** This is display + persistence plumbing; the SDK still feeds the model the real `tool_result`. Screenshots-Read-and-embed and Read behavior are untouched — no seed edit for that.
- **One touchpoint to check, not assume:** any skill that instructs the agent to *read a chat transcript* via `GET /api/chats/{id}` now sees an excerpt for large tool blocks and must fetch `/tool-output/{tool_use_id}` for the full text. Grep `backend/scripts/seed-skills/` for transcript-reading guidance; if none (likely), zero seed changes — but verify rather than assume.

---

**Summary of findings for the caller:** The lazy-tool-output feature is already on `origin/main` (`ToolBlock.jsx`, `routes/chats.py:566-687`, `test_chats_tool_output.py`), but it only trims the `GET /api/chats/{id}` payload — the SSE wire, the catch-up replay burst, and the `Chat.messages` DB row all still carry full output, and the current excerpt is an empty string rather than a head+tail preview. The design above moves truncation to the single event funnel `_ChatEventSink.publish` (`chat.py:284` — confirmed to be the runner's `bc`, so it covers wire + persistence in one place), carries a head+tail excerpt inline, stores the full output in a new `tool_outputs` SQLite table keyed by `tool_use_id` (written via a race-immune actor command, swept with the chat's existing purge, correctly outside the `/data` git safety-net), and serves it via `GET /api/chats/{chat_id}/tool-output/{tool_use_id}`. The load-bearing correctness seam is failure-chip detection (`toolResultFormat.js` parses `output` for the exit code) — the excerpt must preserve the head and JSON-envelope validity, backed by an explicit `output_exit_code` field. Migration is trivial dual-read (keep the existing `?ts=&i=` endpoint for legacy fat rows). Total effort ≈ one focused M+ session.
---

Worktree removed and pruned cleanly. Here is the refreshed addendum.

---

# Chat Behavior Contract v2 — Rule 5 addendum, REFRESHED against current main (`46b20357`)

This supersedes the original Rule-5 mapping (mapped at `ec94f5b`, before two
landings). All file:line refs verified read-only in a detached worktree at
`origin/main` HEAD `46b20357`. It re-runs the original inventory, redesigns
Lever 2 on what now exists, and re-verifies Levers 1 & 3 and the five risk
seams against cid-era code.

## What changed since the original mapping (two landings)

**(1) cid stable identity** (`0fb832ce`/`46d6901a`/`e7d1250e`/`7f142bab`, synthesis `docs/superpowers/specs/2026-07-12-cid-identity-SYNTHESIS.md`). Net effect on Rule 5:
- **User rows are now identity-stable.** React key is `cidOf(msg)`, the row carries `data-cid`, and the optimistic→server-ts swap no longer remounts it (`ChatView.jsx:2958-2966`). Pin is `PIN_USER_MSG{cid}` resolved by `_pinnedUserEl(scrollEl, cid)` → `.chat__msg--user[data-cid]`, no last-row fallback (`useScrollMode.js:110-114,133-137`).
- **The scroll anchor for the answer did NOT change.** ANCHOR_AT still resolves by `data-key` — `_topmostVisibleMsg` scans `.chat__msg[data-key]` (`useScrollMode.js:68-76`), `applyMode` ANCHOR_AT queries `[data-key]` (`:145-151`). User rows now carry BOTH `data-cid` (pin) and `data-key` (anchor). Assistant rows carry only `data-key` (they have no cid). So Lever-1/Lever-2's obligation to keep a stable answer identity is **entirely about `data-key` and the block keys — cid does nothing for the assistant answer.**
- cid touched `useStreamConnection.js` by only +12 lines and did **not** touch `StreamingMessage.jsx`, `MsgContent.jsx`, `streamReducers.js`, `streamPromotion.js`, `InlineContent.jsx`, `blocks.jsx`, or `markdown.css`. The original addendum's claims about those files therefore still hold (line numbers drift ≤ a few).

**(2) Lazy tool output** (`46b20357`, contract item 3). Net effect on Rule 5:
- **`tool_use_id` now rides the wire and the persisted block.** Both runners emit it on `tool_start`/`tool_output`/`tool_end` (`claude_sdk_runner.py:780,869,875` = `block.id`/`block.tool_use_id`; `codex_sdk_runner.py:324-348` `_stamp_tool_use_id` from the ThreadItem id). `events.py:process_event` stamps it onto the persisted block (`tool_start` `:357-359`; `tool_output` backfill `:385-387`).
- **But nothing correlates or keys by it yet.** `events.py` still matches `tool_output`/`tool_end` by **position** ("last non-done", `reversed(assistant_blocks)` `:375,408`), `tool_input` by "earliest without input" (`:368-371`), `tool_sources` by "last WebSearch" (`:401`). The frontend **live** `tool_start` handler does **not** read `event.tool_use_id` — it builds `{type,tool,input,output,status}` and drops the id (`useStreamConnection.js:763-771`). So a live (and client-promoted) tool block has **no** `tool_use_id`; only the DB-reloaded block does. `streamReducers.js` still position-matches (`openToolLifecycleIndex` = "last running", `:129-136,147-153,247-258`). `ToolBlock` uses `tool_use_id` only for the lazy-fetch URL (dual-read: `tool_use_id` → by-id endpoint, else legacy `?ts=&i=`; `ToolBlock.jsx:103-126`), never as a React key.

**Bottom line: the redraw mechanics the original addendum named are intact.** The lazy-tool landing put a real per-tool identity *on the wire and in the DB row* — which is exactly the ingredient Lever 2's "backend-later" half asked for — but the live stream still throws it away and everything is still keyed ordinally.

**(3) New since original mapping: `streamSnapshotCache.js`.** `useStreamConnection` persists visible `streamItems` to sessionStorage (`chat-stream-items:v2:<chatId>`) on every non-empty set and **restores them at init on remount** (`useStreamConnection.js:184-185,206-212,readStoredStreamSnapshot`). This means Path A remount seeds `StreamingMessage` from the snapshot *before* catch-up, softening the DB-only flash — but the snapshot items are plain ordinal objects, and the catch-up commit still `setStreamItems(catchUpItems)` replaces the whole array (`:654-670`). So it is **one more reset+replace surface, not a reconcile** — Lever 2 still applies, and now must also cover the snapshot→catch-up hand-off.

## 1. Remount inventory — re-verified

The **catch-up reset+replay** engine is unchanged: off-screen `catchUpItems` buffer, atomic `commitCatchUp()` → `setStreamItems(catchUpItems)` at `catch_up_done`/`done` (`useStreamConnection.js:641-670,715-721,931-933`). The commit still replaces the whole array with fresh object identities; React reconciles by **ordinal key**.

What cid fixed vs what remains ordinal:

| Surface | Identity today | Verdict |
|---|---|---|
| **User rows** | `cid` (key, `data-cid`, `PIN_USER_MSG{cid}`) | **FIXED by cid** — no remount on ts-swap |
| **Assistant answer container (`<li>`)** | `data-key` = `streamingDataKey` (bridge partial's `id`/`role-ts`, else `streaming-<chatId>`) (`ChatView.jsx:2746-2752,2994`) | **ordinal-ish** — stable across ticks but the live↔DB *surface swap* still swaps the whole subtree |
| **Assistant blocks (text/thinking)** | `s-${i}` / `i` (array index) | **still ordinal** (`StreamingMessage.jsx:129,150,156`; `MsgContent.jsx:97,121`) |
| **Tool blocks** | `s-t-${i}` / `t-${i}` (array index) — id available but unused | **still ordinal** (`StreamingMessage.jsx:111,191`; `MsgContent.jsx:111,224`) |
| History above the active turn | `msg` identity (memo) | **safe** (`MsgContent.jsx:270-288`) |

**The surface swap (§1b) is unchanged and remains the dominant Path-A redraw.** Mount keeps the DB partial and connects while running (`ChatView.jsx:1361-1363`), so the answer is first painted by `MsgContent`; catch-up commit flips `chooseActiveAssistantSurface` → `hideMessage`/`liveMirrorMsgIdx` → the DB `<li>` is suppressed (`ChatView.jsx:2916-2920`) and `StreamingMessage` renders instead (`:2991-2997`). Two subtrees, two key namespaces (`i`/`t-${i}` vs `s-${i}`/`s-t-${i}`), different DOM (`ProgressiveMarkdown` + `.chat__cursor` vs `StandardMarkdown`) → full teardown/rebuild of every `ToolBlock`, `<img>`, KaTeX/highlight node in the answer.

## 2. Layout-shift inventory — re-verified (Lever 3 targets)

Media reserve is **unchanged** (cid didn't touch it): `.md-image-frame { aspect-ratio: var(--md-image-ratio, 4 / 3); max-height: min(60dvh, 480px) }` (`markdown.css:175-184`, exact line match to the original). Residual shifts both still present:
1. **Late token insert.** `ExpandableImage` returns `null` until `resolvedSrc` lands (`InlineContent.jsx:152`); for media-token images that is a second async hop (`:137-150`), so the *whole frame* appears late — no box is reserved during the hop.
2. **On-load ratio delta.** `onLoad` measures `naturalWidth/Height` → overwrites `--md-image-ratio`/`--md-image-fit-width` (`:163-179`); the delta from the 4/3 default still nudges.

Hide-then-reveal cloak is unchanged: `visibility:hidden` until a 50ms-quiet window capped at `REVEAL_CAP_MS=1500` (`useScrollMode.js:45,690-699,837-844`). It still covers *mount paint* only — live streaming never goes quiet (reveals at the 1500ms cap mid-stream), the async catch-up commit lands after reveal, and **Path B (foreground return) has no cloak** — `freezeStreamingReturn` only re-anchors the mode via `modeForForegroundReturn(..., {streaming:true})` (`ChatView.jsx:2476-2495`).

## 3. Levers — refreshed

### Lever 1 — single renderer for the active answer *(biggest win, unchanged direction, re-verified seams)*

Still the dominant fix and still structural. Make one renderer (`MsgContent`/a shared `AssistantBlocks`) render both the DB partial and the live stream; switching source becomes a prop/data change, not a subtree swap. `streamItemsToAssistantPayload` (`streamPromotion.js:253-265`) already maps stream items → the block shape `MsgContent` consumes. Fold `ProgressiveMarkdown`'s only real deltas (trailing cursor, `aria-live`) behind an `isStreaming` flag.

Post-cid adjustments to the plan:
- The surface-selection block moved slightly: consume points are `ChatView.jsx:2628-2631` (choose), `:2916-2920` (suppress DB `<li>`), `:2991-2997` (render stream). `streamingDataKey` at `:2746-2752`.
- **Keep `data-key` identical across the source switch** — this is the scroll-machine contract (seam 5) and it is what cid did *not* touch. In the bridge case `streamingDataKey` already reuses the partial's key; the single-renderer must preserve that so `ANCHOR_AT` resolves before and after the switch.

**Size: M/L.** Touches `MsgContent`, `StreamingMessage` (shrinks to a thin wrapper or is absorbed), the surface-selection block, `ProgressiveMarkdown`. Risk: regressing constraint #9 (single assistant surface) and the bridge/steer promote.

### Lever 2 — stable stream-item identity (REDESIGNED on what now exists)

The lazy-tool landing already put a real per-tool identity on the wire and in the DB row, so Lever 2 gets **cheaper and more robust than the original two-phase plan** — the "backend-later" ingredient is largely already there. Revised, smallest-scheme-first:

**2a — Thread `tool_use_id` onto the live stream item and key tool blocks by it. `S`.**
One line in the live `tool_start` handler to carry `event.tool_use_id` onto the item (`useStreamConnection.js:763-771`), one in `streamItemToBlock` (it already spreads `...item`, so it flows automatically once the item has it — `streamPromotion.js:43`). Then key tool blocks by `t.tool_use_id ?? \`t-${i}\`` in both `StreamingMessage.jsx` and `MsgContent.jsx` (keep the ordinal fallback for legacy/tokenless blocks). This makes tool blocks — the heaviest remount cost (expanded state, `<img>`, lazy `fullOutput`) — survive a catch-up commit and the Lever-1 surface switch **by real identity**, with no protocol change (the wire already carries it).

**2b — Synthetic `iid` for text/thinking segments + the answer container. `S/M`.**
Text/thinking segments have no natural wire id. Give each stream item a monotonic `iid` assigned at first creation (in the reducers/`appendTextChunk`/`appendThinkingChunk`), preserved across the reset+replay, and key those segments by `iid` instead of array index. The answer container `<li>` keeps `data-key = streamingDataKey` (turn-scoped, already stable) — a block-ordinal *within* that stable turn key is fine, because the turn id doesn't change across catch-up. Prefer `tool_use_id` where it exists (2a) and `iid` only for text/thinking.

**2c — Reconcile-into-existing on commit. `M`.**
The core of "return without redraw": make `commitCatchUp` **merge fields into the existing items by key** (tool_use_id / iid) instead of `setStreamItems(freshArray)` (`useStreamConnection.js:654-670`). The replay is deterministic and in-order, so `catchUpItems[k]` ↔ on-screen item `k`; merging preserves object/DOM identity through the commit. Must also cover the **snapshot→catch-up** hand-off (2 above): the snapshot-seeded items get the same keys, so the first commit reconciles onto them rather than replacing.

**2d — (optional, later) switch backend + reducer correlation from position to id. `M`.**
`events.py` and `streamReducers.js` still match by "last non-done". With `tool_use_id` now on every tool event, switch `attachToolOutput`/`closeToolLifecycle`/`tool_output`/`tool_end` to id-match. Not required for Rule 5 (serial invariant holds today, `events.py:1-10`), but it hardens 2a against any future parallel-tool interleaving and removes the last position dependence. Do it only if 2c surfaces an ordering fragility.

**Do 2a + 2c first** (smallest scheme that makes catch-up reconcile-into-existing possible); 2b covers the remaining text/thinking churn; 2d is hardening.

### Lever 3 — dimension-stable media + cloak the commit (unchanged direction, re-verified)

- **Reserve the frame before the token resolves.** Render `.md-image-frame` immediately, swap the `<img>` in when `resolvedSrc` lands, rather than returning `null` for the whole component (`InlineContent.jsx:152`). **`S`.**
- **Carry known screenshot dimensions.** The agent already knows the viewport it was handed (`useStreamConnection.js:1152-1160`); carry width/height in the image markup so `--md-image-ratio` is exact on first paint and the on-load delta disappears. **`S`.**
- **Cloak the first catch-up commit.** Extend hide-then-reveal (or freeze the anchor) across the first post-reconnect commit, covering Path B (no cloak today) and the async Path-A commit after the 1500ms cap. Gate on "the socket reset and a catch-up burst is inbound" using the existing `catchUpStartedRef`/`reconnecting` signals + `QUICK_WAKE_HIDDEN_MS`/kept-socket logic (`useStreamConnection.js:44,200,1279-1360`) so a glance at the notification shade doesn't blink the chat. **`S/M`.**

### Simplest thing that satisfies Rule 5

**Lever 2a + 2c (reconcile tool blocks + merge-on-commit) + Lever 1 (single renderer).** With `tool_use_id` already on the wire, 2a is now a two-line change, and 2c turns the commit into an in-place merge; Lever 1 removes the surface swap. Together they make "return mid-stream" an in-place prop update of the same DOM nodes. 2b (iid) polishes text/thinking; Lever 3 handles residual pixel settle + Path B's missing cloak.

## Revised implementation plan (sizes)

| Piece | Size | Notes |
|---|---|---|
| L2a — carry `tool_use_id` onto live item + key tool blocks by it | **S** | `useStreamConnection.js:763-771`, `StreamingMessage.jsx:111,191`, `MsgContent.jsx:111,224`; wire already carries it (both runners + `events.py`), so no protocol change. Ordinal fallback stays for legacy blocks |
| L2c — reconcile-into-existing at `commitCatchUp` (+ snapshot hand-off) | **M** | `useStreamConnection.js:654-670`; merge by key, don't replace; cover snapshot-seeded items |
| L2b — synthetic `iid` for text/thinking + key by it | **S/M** | reducers/`appendTextChunk`/`appendThinkingChunk`; container keeps `data-key` |
| L1 — single renderer for the active answer | **M/L** | `MsgContent`/`AssistantBlocks` fed by `streamItemsToAssistantPayload`; absorb `StreamingMessage`; `isStreaming` flag for cursor/aria-live |
| L3 — frame-before-token + carry known dims | **S** | `InlineContent.jsx:129-189`, `markdown.css:175-184` |
| L3 — cloak first reconnect commit | **S/M** | reuse reveal machinery gated on `catchUpStartedRef`/`reconnecting` |
| L2d — id-match correlation (backend + reducers) | **M** | optional hardening; `events.py:368-425`, `streamReducers.js:129-153,247-258` |

## Risk seams — re-verified against cid-era code

1. **Dedup on reconnect.** Catch-up replays the whole run. Reconcile-by-key (2c) must map replayed item `k` onto existing item `k` and not append duplicates; verify the `text_final` repair (`useStreamConnection.js:379-389,736-745`), `upsertQuestionItem` (`streamReducers.js:97-120`), **and now the `streamSnapshotCache` restore** (the first commit reconciles onto snapshot-seeded items) and `commitCatchUp`'s empty-replay-authoritative branch (`:660-666`).
2. **Ordering / interleaving.** Serial-tool invariant still asserted (`events.py:1-10`). Position-match survives in `events.py` (`:375,408`) and `streamReducers.js` (`openToolLifecycleIndex`). L2a keys by id but leaves correlation positional — safe under the serial invariant; L2d closes it if that ever breaks.
3. **Stop / steer.** Steer seals the pre-steer segment (`ChatView.jsx` promote path) and catch-up **drops** the replayed pre-steer buffer (`useStreamConnection.js:912-923`, `catchUpItems=[]`). A reconcile must not resurrect dropped pre-steer items. The steered user row is now cid-minted (`ChatView.jsx:967-975`) and `insertMessageBatchByTs` still dedups by **ts** (cid adjudication #1 kept ts for display ordering) — keep that guard; identity-keying the *stream* must not disturb the *message-list* ts dedup.
4. **Bridge boundary.** `bridgeTs`/`findBridgeIndex` decide REPLACE-vs-APPEND at promote; bridge identity is **ts-based** for the assistant row (`streamPromotion.js:304` `m.ts === bridgeTs`; assistant rows have no cid — cid did not change this). `streamingDataKey` (`ChatView.jsx:2746-2752`) is the scroll anchor. If L1 makes the live surface *be* `MsgContent` over the same `msg`, confirm the promote still replaces (not appends) and `streamingDataKey` resolves to the same node before/after the switch.
5. **Scroll-machine coupling.** `applyMode` ANCHOR_AT queries `[data-key]`, `_topmostVisibleMsg` scans `.chat__msg[data-key]` (`useScrollMode.js:68-76,145-151`); the streaming `<li>` carries `data-key={streamingDataKey}` (`StreamingMessage.jsx:180-183`, `ChatView.jsx:2994`). **Unchanged by cid** — cid added `data-cid` for the *pin*, but the *anchor* is still `data-key`. Any change to which `<li>` renders the answer (L1) must keep a stable `data-key` across the return, or ANCHOR_AT/leave-return restore (constraint #7) regresses.

## Test anchors (all present at `46b20357`)

Extend, don't invent: `__tests__/streamSnapshotCache.test.js`, `streamPromotion.test.js`, `streamReducers.test.js`, `streamingRobustness.test.js`, `groupBlocks.test.js`, `imageReserveHeight.test.js`, `mediaImageEmbed.test.js`, `useScrollMode.test.js`, `chatRuntimeState.test.js`; Playwright `tests/stream-reconnect.spec.mjs`, `spacer.spec.mjs`, `second-send-pin.spec.mjs`, `pin-clamp-settle.spec.mjs`. Add: (a) a reconcile test asserting **object identity is preserved** across a catch-up commit for unchanged items (both tool_use_id-keyed and iid-keyed); (b) a surface-switch test asserting `ToolBlock.open` and `<img>` identity survive a mid-stream return; (c) a snapshot→catch-up test asserting the first commit reconciles onto snapshot-seeded items (no duplicate append); (d) a media-dimension test asserting the frame reserves before `resolvedSrc` and known-dimension markup eliminates the on-load delta.

**Files of record (absolute, current main):** `/home/hmzmrzx/projects/mobius/frontend/src/components/ChatView/{useStreamConnection.js, ChatView.jsx, StreamingMessage.jsx, MsgContent.jsx, ToolBlock.jsx, streamPromotion.js, streamReducers.js, streamSnapshotCache.js, useScrollMode.js, groupBlocks.js, markdown/InlineContent.jsx, markdown/blocks.jsx, markdown.css}`, `.../backend/app/{events.py, claude_sdk_runner.py, codex_sdk_runner.py}`, `.../frontend/src/components/Shell/Shell.jsx:1696-1698`.