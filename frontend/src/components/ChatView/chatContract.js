/**
 * chatContract — the machine-checkable definition of the chat-UX invariants
 * that CLAUDE.md declares "non-negotiable" (see "Chat UX — non-negotiable
 * constraints"). One pure module, three consumers: node unit tests, Playwright
 * `page.evaluate`, and (later) a runtime monitor on prod.
 *
 * Purity is the whole point. No React import, no `window` / `document`, no DOM
 * queries: every input arrives as an argument (a real element or a plain object
 * exposing the same numeric fields), so the identical code runs in node against
 * fake geometry and injected into a live browser context. Missing elements
 * yield a snapshot of explicit nulls and predicates that report `ok:false` with
 * a reason — never a throw, because a monitor that crashes on a half-rendered
 * frame is worse than one that reports "indeterminate".
 *
 * CONSTANTS-SYNC DECISION: PIN_OFFSET / PIN_BOTTOM_ROOM are re-declared here,
 * not imported from useScrollMode.js. They are module-private there (not
 * exported), and importing anything from that file would pull React and a
 * module-load sessionStorage read into this module — breaking both the
 * no-React-import rule and browser-injectability. The mirror is the correct
 * trade. SYNC OBLIGATION: if these change in useScrollMode.js, change them here
 * too. They are exposed as predicate parameters (defaulting to the mirror) so a
 * caller can override without editing this file.
 */

// Mirror of the load-bearing constants in ChatView/useScrollMode.js.
export const PIN_OFFSET = 4
export const PIN_BOTTOM_ROOM = 180

/**
 * The geometry-checkable subset of the CLAUDE.md constraints. Each id is the
 * `id` a predicate stamps on its result, so a violation is traceable back to
 * the owner law it broke.
 */
export const CHAT_CONTRACT = [
  {
    id: 'pin-on-send',
    title: 'Pin to top on send',
    summary:
      'A new user message scrolls flush to the top of the viewport so the '
      + 'reply streams in below it, terminal-style. The owner calls this pin '
      + '"holy".',
  },
  {
    id: 'pin-holds-streaming',
    title: 'Pinned row holds while streaming grows',
    summary:
      'Once pinned, the message stays at the top with a bottom cushion as '
      + 'content streams in above or below it — no drift without a user gesture.',
  },
  {
    id: 'no-scroll-on-read-send',
    title: 'No scroll movement on not-at-bottom send/steer',
    summary:
      'Auto-follow is OFF after every send. A send or steer while the reader is '
      + 'scrolled up must leave scrollTop untouched — never yank the reader.',
  },
  {
    id: 'spacer-reserves-room',
    title: 'Spacer reserves reachable room with bottom cushion',
    summary:
      'The dynamic spacer, sized from fullViewH not clientHeight, reserves '
      + 'enough that the pin target is reachable and leaves real room below it, '
      + 'so the message lands at the top instead of stranded mid-viewport.',
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
      + 'the response. Checked from a caller-supplied selector count.',
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
  const { scrollEl, listEl, lastUserMsgEl, fullViewH } = env || {}
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
    fullViewH: fullViewH ?? null,
  }
}

function indeterminate(id, expected, reason) {
  return { ok: false, id, measured: null, expected, reason }
}

/** Shared shape for the two "pinned gap must not drift" invariants. */
function gapPreserved(id, before, after, tolerance) {
  const expected = `|Δ pinGap| <= ${tolerance}`
  if (!before || !after || before.pinGap == null || after.pinGap == null) {
    return indeterminate(id, expected,
      'no pinGap in before/after (missing user message)')
  }
  const drift = after.pinGap - before.pinGap
  return {
    ok: Math.abs(drift) <= tolerance, id, expected,
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

/** C2 pin-holds-streaming: the pin did not drift as content grew. */
export function pinHeld(before, after, { tolerance = 8 } = {}) {
  return gapPreserved('pin-holds-streaming', before, after, tolerance)
}

/** C5 reanchor-on-promote: the pin did not jump when items promoted. */
export function reanchored(before, after, { tolerance = 8 } = {}) {
  return gapPreserved('reanchor-on-promote', before, after, tolerance)
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

/** C4 spacer-reserves-room: reserved room below the pin >= the cushion. */
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
 *  checks — the shape a unit test asserts on and a runtime monitor logs. */
export function checkContract(checks) {
  const list = Array.isArray(checks) ? checks : []
  const violations = list.filter(c => !c || !c.ok)
  return { ok: violations.length === 0, violations }
}
