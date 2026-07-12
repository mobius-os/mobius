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
