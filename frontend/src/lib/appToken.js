/**
 * Token selection for an in-shell mini-app (AppCanvas).
 *
 * Pulled out of AppCanvas.jsx as a pure function so the offline/flap
 * behaviour is unit-testable without a browser — it encodes a fix for a
 * spinner-forever bug that only reproduced on a real Android PWA and was
 * invisible to every desktop harness.
 *
 * THE BUG IT FIXES
 * On an installed Android PWA in airplane mode, the service worker serves the
 * shell from cache, so the browser never makes a real network request and
 * `navigator.onLine` reports a STALE `true`. The reachability probe (which
 * actually hits /api/health) correctly says offline, so the two signals
 * disagree and the derived `online` value OSCILLATES true↔false. The old
 * selection was:
 *
 *     token = appToken || (online ? undefined : ownerToken)
 *
 * Each pass through `online === true` with no app-scoped token (there never is
 * one offline) made `token` undefined, which tripped AppCanvas's `if (!token)`
 * branch and UNMOUNTED a fully-mounted iframe. The next flip remounted a fresh
 * iframe that had to re-handshake and never finished → spinner forever.
 * Desktop never hit this because desktop `navigator.onLine` is reliable.
 *
 * THE FIX — latch
 * Once a usable token has been resolved, keep it: a transient `online === true`
 * blip must not revoke a token we already legitimately chose and tear down the
 * live app. The latch only ever holds a value the live selection itself
 * produced (an app-scoped token while genuinely online, or the owner JWT once
 * reachability reported offline) — so it does NOT weaken the security intent of
 * "never substitute the owner JWT during a real online session." Callers reset
 * the latch (pass latched = undefined) when the iframe is torn down for a real
 * reason (different appId or version bump) so a new app can't inherit the
 * previous one's token.
 */

/**
 * The live (un-latched) token choice for this render.
 *
 *   • online  → wait for the app-scoped token; never substitute the owner JWT
 *     (keeps the long-lived owner JWT out of the module URL during a genuine
 *     online session).
 *   • offline → fall back to the owner JWT so a fully-cached offline-capable
 *     app still boots. The iframe is same-origin and can already read this JWT
 *     (documented sandbox trade-off), so passing it is not a new exposure.
 *
 * @param {string|undefined|null} appToken app-scoped token, or falsy if none
 * @param {boolean} online real reachability (NOT navigator.onLine)
 * @param {string|undefined|null} ownerToken owner JWT from localStorage
 * @returns {string|undefined} the token to use this render, or undefined
 */
export function liveAppToken(appToken, online, ownerToken) {
  if (appToken) return appToken
  if (online) return undefined
  return ownerToken || undefined
}

/**
 * The latched token: prefer the freshest app-scoped token, otherwise hold the
 * latch so an `online` oscillation can't revoke a token already resolved.
 *
 * @param {string|undefined|null} appToken freshest app-scoped token
 * @param {string|undefined|null} latched previously-resolved token (the latch)
 * @returns {string|undefined} the token AppCanvas should use this render
 */
export function latchedAppToken(appToken, latched) {
  return appToken || latched || undefined
}

// MODULE-LEVEL latch store, keyed by `${appId}:${version}`.
//
// A React useRef is NOT enough: the on-device log showed the token dropping
// back to NONE-blank AFTER the app had mounted, which means AppCanvas itself
// REMOUNTS during the offline flap (a remount resets every useRef, evaporating
// the latch). Persisting the resolved token at module scope lets it survive an
// AppCanvas remount for the SAME app+version. The compound key keeps each app's
// token isolated — a different app simply reads a different key and never
// inherits another's token, so NO cross-key deletion is needed (and must NOT be
// done: the Shell mounts up to 4 AppCanvas siblings at once via the iframe LRU,
// so deleting "other" keys on each sibling render would clobber the others'
// latches by render order). We only drop STALE VERSIONS of the SAME app (a
// version bump is a real teardown) and cap the map as a backstop.
const _latchStore = new Map()
const _LATCH_CAP = 16

function _latchKey(appId, version) {
  return `${appId}:${version}`
}

/**
 * Resolve the token for AppCanvas, latching across remounts. Call once per
 * render with the live (un-latched) choice; it stores any non-empty live token
 * under the app+version key and returns the best available (fresh app token >
 * latched). Drops older-version latches for the SAME app, but leaves other
 * apps' latches intact (the 4-up iframe LRU mounts siblings concurrently).
 *
 * @param {string|number} appId
 * @param {string|number} version
 * @param {string|undefined} liveToken the un-latched choice this render
 * @param {string|undefined|null} appToken freshest app-scoped token
 * @returns {string|undefined}
 */
export function resolveLatchedToken(appId, version, liveToken, appToken) {
  const key = _latchKey(appId, version)
  const samePrefix = `${appId}:`
  // A version bump is a real teardown for THIS app — forget its older versions
  // so a remount can't reuse a stale-version token. Leave OTHER apps alone.
  for (const k of [..._latchStore.keys()]) {
    if (k !== key && k.startsWith(samePrefix)) _latchStore.delete(k)
  }
  if (liveToken) {
    _latchStore.set(key, liveToken)
    // Backstop against unbounded growth if apps churn without logout.
    if (_latchStore.size > _LATCH_CAP) {
      const oldest = _latchStore.keys().next().value
      if (oldest !== key) _latchStore.delete(oldest)
    }
  }
  return latchedAppToken(appToken, _latchStore.get(key))
}

// Clear all latched tokens. Called on logout so a remount after a session ends
// can't reuse the previous owner's token (the store is owner-scoped state, like
// the SW caches client.js wipes).
export function clearLatchedTokens() {
  _latchStore.clear()
}

// Test-only alias.
export const _resetLatchStore = clearLatchedTokens
