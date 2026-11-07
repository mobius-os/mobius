/* Who owns a passive-requested block, and whether that owner shows the
   passive or the static form. Pure, so both decisions are testable alone. */

/** Which component owns the block. A passive session needs the block's
 *  request and, to start, the app's live admission. Once started it stays the
 *  single owner of the block, whatever later app-list refreshes say: if
 *  admission is withdrawn it decides from its own state whether it is held
 *  (an unresolved outcome) or shows the static form, so no outcome lock is
 *  ever dropped by a parent deciding from a stale copy. */
export function appBlockRoute({ requested, installed, admitted, started }) {
  return requested && installed && (admitted || started) ? 'passive' : 'static'
}

/** Static unless an admitted, supporting session has a frame slot. A view
 *  the reader opened in the static form stays until the reader closes it. An
 *  unresolved (retained) session always stays, even when admission is
 *  withdrawn: it is then held, with no new action, until it settles. This is
 *  decided here, from the session's own synchronous state. */
export function passiveBlockMode({ admitted = true, supported, near, granted, retained, staticOpen }) {
  if (retained) return 'passive'
  return !admitted || supported === false || staticOpen || (near && !granted) ? 'static' : 'passive'
}
