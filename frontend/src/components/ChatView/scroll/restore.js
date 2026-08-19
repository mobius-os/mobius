/** Validation and normalization for durable chat reading anchors. */

import { PIN_OFFSET } from '../chatContract.js'
import { cidOf } from '../messageIdentity.js'
import { isOwnerUserMessage } from '../chatRuntimeState.js'
import {
  _anchorEl,
  _anchorModeIntersectsContent,
  _durableQuestionSubmissionMode,
  _pinnedUserEl,
  bottomAnchorModeFromScroll,
} from './geometry.js'

/** Validates a saved ScrollMode against current state. A valid reader anchor
 * is exact. With no resolvable location, show the latest real content once as
 * a settled ANCHOR_AT — never FOLLOW_BOTTOM. */
export function _validateSavedMode(saved, messages, scrollEl) {
  const holdBottom = () => bottomAnchorModeFromScroll(scrollEl) || { kind: 'INITIAL' }
  if (!saved || !saved.kind) return holdBottom()
  if (saved.kind === 'FOLLOW_BOTTOM') return holdBottom()
  if (saved.kind === 'PIN_USER_MSG') {
    // A save without a cid (malformed, or written by pre-cid code) can't
    // resolve a pin target — use the explicit no-location fallback.
    if (saved.cid == null) return holdBottom()
    const lastUserMsg = [...messages].reverse()
      .find(isOwnerUserMessage)
    if (cidOf(lastUserMsg) !== saved.cid) return holdBottom()
    // PIN_USER_MSG is a live send action, not a durable reading location.
    // Restore its physical result as an ordinary anchor so mount/return cannot
    // recreate pin authority or its later pin→follow layout handoff.
    const row = _pinnedUserEl(scrollEl, saved.cid)
    return row?.dataset?.key
      ? { kind: 'ANCHOR_AT', key: row.dataset.key, offset: PIN_OFFSET }
      : holdBottom()
  }
  if (saved.kind === 'ANCHOR_AT') {
    // A resolvable row is not enough: an old build could persist that row with
    // a huge negative offset while the viewport sat wholly in spacer below it.
    // Enforce the same content-intersection invariant used by spacer sizing,
    // self-healing every off-content restore to the real tail.
    const durable = _durableQuestionSubmissionMode(saved)
    const target = _anchorEl(scrollEl, durable)
    return _anchorModeIntersectsContent(target, durable, scrollEl?.clientHeight)
      ? durable
      : holdBottom()
  }
  return holdBottom()
}

/** Decide how the entry (restore) gate should act for the current mode.
 *
 * The gate converts the neutral INITIAL mode into a concrete reading
 * coordinate exactly once per activation. `_validateSavedMode` only yields
 * INITIAL when there is no content row to address yet — its tail fallback
 * needs at least one `.chat__msg[data-key]` in the DOM. Committing that
 * INITIAL would resolve the coordinate to a no-op and reveal the transcript at
 * scrollTop 0 (the physical top) with no later re-resolution, which is the
 * reported "keep being taken to the top of a chat". So a not-yet-addressable
 * transcript returns `wait`: hold INITIAL and let a later paint (effect re-run,
 * ResizeObserver, or reveal) resolve it against real rows.
 *
 * Returns one of:
 *   { action: 'idle' }                     — not in a restore position
 *   { action: 'wait', resolved, savedPresent }
 *                                          — cannot resolve yet; keep waiting
 *   { action: 'commit', mode, resolved, savedPresent }
 *                                          — concrete restore coordinate
 */
export function entryRestoreDecision({ mode, saved, messages, scrollEl, phase }) {
  const savedPresent = !!saved
  const restorePhase = phase === 'cache-validating'
    || phase === 'cached'
    || phase === 'ready'
  if (mode?.kind !== 'INITIAL' || !restorePhase) {
    return { action: 'idle', resolved: false, savedPresent }
  }
  const restored = _validateSavedMode(saved, messages, scrollEl)
  // No addressable row yet — revealing now would strand the reader at the top.
  if (restored.kind === 'INITIAL') {
    return { action: 'wait', resolved: false, savedPresent }
  }
  const resolved = savedPresent && !restored.defaultTail
  // cache-validating reveals only on an authoritative saved coordinate; a
  // manufactured tail fallback must wait for the validated window.
  if (phase === 'cache-validating' && !resolved) {
    return { action: 'wait', resolved, savedPresent }
  }
  return { action: 'commit', mode: restored, resolved, savedPresent }
}


/** Normalize durable reader locations without collapsing live mode state.
 *
 * FOLLOW_BOTTOM and PIN_USER_MSG are useful while this mount is active and
 * are already converted to settled restore modes by `_validateSavedMode` on
 * the next mount. ANCHOR_AT can still carry legacy off-content geometry, so
 * validate that location before every write. */
export function _modeForPersistence(mode, messages, scrollEl) {
  return mode?.kind === 'ANCHOR_AT'
    ? _validateSavedMode(mode, messages, scrollEl)
    : mode
}
