/**
 * chatContract — the machine-checkable subset of the owner-authoritative
 * invariants in ARCHITECTURE.md "Chat scroll + steer contract". This pure
 * module is currently exercised by node unit tests and is deliberately safe
 * to import from Playwright `page.evaluate` or a future runtime monitor. Those
 * browser/runtime consumers are not implemented yet; do not count them as
 * present contract coverage until tracked callers exist.
 *
 * Purity is the whole point. No React import, no `window` / `document`, no DOM
 * queries: every input arrives as an argument (a real element or a plain object
 * exposing the same numeric fields), so the identical code runs in node against
 * fake geometry and injected into a live browser context. Missing elements
 * yield a snapshot of explicit nulls and predicates that report `ok:false` with
 * a reason — never a throw, because a monitor that crashes on a half-rendered
 * frame is worse than one that reports "indeterminate".
 *
 * INJECTION CONTRACT: the module is injected or bundled WHOLE into a browser
 * context (vite import, or injecting the file's text and evaluating it as a
 * module). Individual functions are NOT serializable through a bare
 * page.evaluate(fn) — they close over module scope (constants, helpers).
 *
 * PREDICATE SELECTION is the caller's job, keyed on submit-time intent: a
 * physical-tail send is judged by pinLanded/pinHeld; a reader-held send or
 * steer by scrollUnmoved. pinLanded deliberately cannot know whether a given
 * send SHOULD have pinned — the caller captures that at submit time.
 *
 * OUT OF SCOPE: transient ordering/jitter and the hide-then-reveal blank
 * window belong to the replay harness and runtime monitor (.pm 210/208).
 *
 * PIN_OFFSET / PIN_BOTTOM_ROOM live here because both the pure contract and
 * the DOM geometry owner consume them. Keeping one React-free definition
 * removes the old source-text sync test and makes drift impossible.
 */

export const PIN_OFFSET = 4
// Extra reservable room below the pin, ON TOP of what's needed to reach it.
// 0 => the spacer reserves exactly enough to pin the message at the top
// (maxScrollTop == pinTarget), with no extra blank below the last content.
export const PIN_BOTTOM_ROOM = 0

/**
 * Machine-readable owner rules from the architecture contract. Geometry
 * predicates stamp their rule's `id` on results; interaction-only rules are
 * enforced by focused policy and browser tests instead of duplicating their
 * state machine here.
 */
export const CHAT_CONTRACT = [
  {
    id: 'pin-on-send',
    title: 'Pin eligible sends to top',
    summary:
      'The first user message always pins. A subsequent direct, queued, or '
      + 'steered message pins only from the physical tail. Reserved '
      + 'reply room remains part of that distance, so any upward reader escape '
      + 'keeps the next send at the current reading position.',
  },
  {
    id: 'follow-at-physical-tail',
    title: 'Re-engage follow at the physical tail',
    summary:
      'Nested controls keep input they can consume. Otherwise an end-directed '
      + 'wheel, scroll key, or touch gesture at the physical tail enters '
      + 'FOLLOW_BOTTOM even when no scroll event fires, without invalidating '
      + 'a delayed send decision.',
  },
  {
    id: 'pin-holds-streaming',
    title: 'Pinned row holds while streaming grows',
    summary:
      'Once pinned, the message stays at the top as content streams in above '
      + 'or below it — no drift without a user gesture.',
  },
  {
    id: 'no-scroll-on-read-send',
    title: 'No scroll movement on not-at-bottom send/steer',
    summary:
      'A subsequent send or steer submitted outside auto-scroll must leave '
      + 'scrollTop untouched — never yank the reader.',
  },
  {
    id: 'spacer-reserves-room',
    title: 'Spacer reserves exactly enough to reach the pin',
    summary:
      'The dynamic spacer uses the active scroll-box height and reserves exactly '
      + 'enough that the pin target is reachable (cushion >= 0), so the message '
      + 'lands flush at the top without scrollable blank room beyond that target. '
      + 'A keyboard therefore removes hidden reservation before moving content.',
  },
  {
    id: 'reanchor-on-promote',
    title: 'Re-anchor on promote without layout jump',
    summary:
      'When streaming ends and items promote to messages the DOM restructures; '
      + 'scrollTop re-anchors so the pinned message does not visibly jump.',
  },
  {
    id: 'single-assistant-surface',
    title: 'Single visible assistant surface',
    summary:
      'Exactly one streaming assistant surface is mounted per turn — the '
      + 'catch-up burst replays all prior events, so a missed reset duplicates '
      + 'the response. Checked from a caller-supplied selector count: this '
      + 'catches duplicates/absence only. Surface IDENTITY (the right surface '
      + 'chosen) is covered by chooseActiveAssistantSurface\'s own unit tests.',
  },
  {
    id: 'continuous-steer-replay',
    title: 'Render exact steer replay as one continuous answer',
    summary:
      'A durable steer boundary may suppress only the exact assistant-text '
      + 'prefix already visible above its user row. Stored content is unchanged; '
      + 'ordinary sends, mismatches, and unsafe Markdown cuts render in full.',
  },
  {
    id: 'anchor-held-through-toggle',
    title: 'Hold the reader through disclosure toggles',
    summary:
      'Collapsing or expanding thinking, tool, and activity disclosures keeps '
      + 'the reader anchor fixed. The helper never writes spacer; the sole '
      + 'owner recomputes the stable latest-turn deficit.',
  },
]

/**
 * Reduce the four elements of the chat scroll subsystem to a flat, serializable
 * snapshot of the geometry the invariants depend on. `env` fields may be real
 * elements or plain objects exposing scrollTop / scrollHeight / clientHeight /
 * offsetHeight / offsetTop. Any missing element leaves its derived fields null.
 * Timestamps are the caller's job — a snapshot is pure geometry.
 */
export function snapshotChatUX(env, { pinOffset = PIN_OFFSET } = {}) {
  const { scrollEl, listEl, lastUserMsgEl } = env || {}
  const scrollTop = scrollEl ? scrollEl.scrollTop : null
  const scrollHeight = scrollEl ? scrollEl.scrollHeight : null
  const clientHeight = scrollEl ? scrollEl.clientHeight : null
  const listHeight = listEl ? listEl.offsetHeight : null
  const lastUserTop = lastUserMsgEl ? lastUserMsgEl.offsetTop : null

  // pinGap is the pinned message's visual distance from the viewport top; at a
  // clean pin it equals pinOffset.
  const pinGap = lastUserTop != null && scrollTop != null
    ? lastUserTop - scrollTop : null
  const distanceToBottom = scrollHeight != null && scrollTop != null
    && clientHeight != null
    ? scrollHeight - scrollTop - clientHeight : null

  // spacerReachable answers "can the pin target be scrolled to right now?" —
  // the reserved room must let the message reach its top slot. The -1 mirrors
  // useScrollMode's targetReachable tolerance.
  const pinTarget = lastUserTop != null
    ? Math.max(0, lastUserTop - pinOffset) : null
  const maxScrollTop = scrollHeight != null && clientHeight != null
    ? scrollHeight - clientHeight : null
  const spacerReachable = pinTarget != null && maxScrollTop != null
    ? maxScrollTop >= pinTarget - 1 : null

  return {
    scrollTop, scrollHeight, clientHeight, listHeight, lastUserTop,
    pinGap, distanceToBottom, spacerReachable,
  }
}

function indeterminate(id, expected, reason) {
  return { ok: false, id, measured: null, expected, reason }
}

/** Shared core for the two "pinned row stays at the top" invariants. Zero
 *  drift alone is not enough — a row parked mid-viewport with a steady gap is
 *  still a violation — so ok requires the AFTER gap to sit at the pin target.
 *  Drift and before/after gaps stay in `measured` for diagnostics. */
function pinStillAtTop(id, before, after, tolerance, pinOffset) {
  const expected = `|after.pinGap - ${pinOffset}| <= ${tolerance}`
  if (!before || !after || before.pinGap == null || after.pinGap == null) {
    return indeterminate(id, expected,
      'no pinGap in before/after (missing user message)')
  }
  const drift = after.pinGap - before.pinGap
  return {
    ok: Math.abs(after.pinGap - pinOffset) <= tolerance, id, expected,
    measured: { drift, before: before.pinGap, after: after.pinGap },
  }
}

/** C1 pin-on-send: the sent message landed flush at the top. */
export function pinLanded(snap, { tolerance = 8, pinOffset = PIN_OFFSET } = {}) {
  const id = 'pin-on-send'
  const expected = `pinGap ≈ ${pinOffset} (±${tolerance})`
  if (!snap || snap.pinGap == null) {
    return indeterminate(id, expected,
      'no pinGap (missing scroll element or user message)')
  }
  return {
    ok: Math.abs(snap.pinGap - pinOffset) <= tolerance,
    id, measured: snap.pinGap, expected,
  }
}

/** C2 pin-holds-streaming: the row is still AT the top after content grew. */
export function pinHeld(before, after, { tolerance = 8, pinOffset = PIN_OFFSET } = {}) {
  return pinStillAtTop('pin-holds-streaming', before, after, tolerance, pinOffset)
}

/** C5 reanchor-on-promote: the row is still AT the top after promote. */
export function reanchored(before, after, { tolerance = 8, pinOffset = PIN_OFFSET } = {}) {
  return pinStillAtTop('reanchor-on-promote', before, after, tolerance, pinOffset)
}

/** C3 no-scroll-on-read-send: a scrolled-up send/steer left scrollTop put. */
export function scrollUnmoved(before, after, { tolerance = 8 } = {}) {
  const id = 'no-scroll-on-read-send'
  const expected = `|Δ scrollTop| <= ${tolerance}`
  if (!before || !after || before.scrollTop == null || after.scrollTop == null) {
    return indeterminate(id, expected,
      'no scrollTop in before/after (missing scroll element)')
  }
  const drift = after.scrollTop - before.scrollTop
  return {
    ok: Math.abs(drift) <= tolerance, id, expected,
    measured: { drift, before: before.scrollTop, after: after.scrollTop },
  }
}

/** C4 spacer-reserves-room: reserved room below the pin >= the cushion
 *  (default cushion PIN_BOTTOM_ROOM = 0, i.e. the pin is exactly reachable;
 *  a negative measured cushion means the pin is stranded below the top). */
export function cushionPresent(snap, { min = PIN_BOTTOM_ROOM, pinOffset = PIN_OFFSET } = {}) {
  const id = 'spacer-reserves-room'
  const expected = `cushion >= ${min}`
  if (!snap || snap.scrollHeight == null || snap.clientHeight == null
    || snap.lastUserTop == null) {
    return indeterminate(id, expected,
      'cannot derive cushion (missing scroll element or user message)')
  }
  const pinTarget = Math.max(0, snap.lastUserTop - pinOffset)
  const cushion = (snap.scrollHeight - snap.clientHeight) - pinTarget
  return { ok: cushion >= min, id, measured: cushion, expected }
}

/** C7 anchor-held-through-toggle: collapsing/expanding a disclosure (a thinking
 *  block, tool card, or activity stretch) must not move the reader. The anchor's
 *  viewport-top stays put across the toggle (±tolerance). The disclosure helper
 *  never writes spacer; the scroll owner may independently consume/restore the
 *  stable latest-turn deficit. */
export function anchorHeldThroughToggle(
  before, after, { tolerance = 2 } = {}) {
  const id = 'anchor-held-through-toggle'
  const expected = `anchor top drift <= ${tolerance}`
  if (!before || !after || before.anchorTop == null || after.anchorTop == null) {
    return indeterminate(id, expected,
      'no anchorTop in before/after (missing disclosure anchor)')
  }
  const drift = after.anchorTop - before.anchorTop
  return {
    ok: Math.abs(drift) <= tolerance,
    id, expected,
    measured: {
      drift,
      spacerBefore: before.spacerH,
      spacerAfter: after.spacerH,
    },
  }
}

/** Single visible assistant surface. Count is supplied by the caller (a DOM
 *  selector count) — this module never touches the DOM. */
export function singleAssistantSurface(count, { expected = 1 } = {}) {
  const id = 'single-assistant-surface'
  if (typeof count !== 'number') {
    return indeterminate(id, `count === ${expected}`, 'no count supplied')
  }
  return { ok: count === expected, id, measured: count, expected }
}

/** Aggregate any set of predicate results into one verdict plus the failing
 *  checks — the shape a unit test asserts on and a runtime monitor logs.
 *  Fails closed on missing evidence: an empty or non-array check list means
 *  nothing actually ran (failed injection, skipped monitor setup) and must
 *  never read as green. */
export function checkContract(checks) {
  if (!Array.isArray(checks) || checks.length === 0) {
    return {
      ok: false,
      violations: [{
        ok: false, id: 'contract-no-evidence', measured: null,
        expected: 'at least one check', reason: 'no checks supplied',
      }],
    }
  }
  const violations = checks.filter(c => !c || !c.ok)
  return { ok: violations.length === 0, violations }
}
