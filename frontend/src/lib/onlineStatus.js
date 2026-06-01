/**
 * Pure connectivity-resolution logic, split out of the useOnlineStatus React
 * hook so it's unit-testable without React or a browser (the same pattern as
 * lib/appToken.js). The hook owns the effect/probe plumbing; this owns the
 * decision.
 */

/**
 * Resolve the next `online` value from a reachability-probe result + the
 * navigator.onLine hint, applying the ASYMMETRY that fixes the slow Android
 * offline cold-open:
 *
 *   • A probe SUCCESS only promotes to online if navigator.onLine isn't
 *     explicitly false. On a real Android installed PWA the probe can briefly
 *     succeed while genuinely offline (a stale keep-alive socket / radio
 *     teardown answering one request); honoring it would yank the app back
 *     "online", re-arm the app-token gate, and stall the mini-app for seconds.
 *     navigator.onLine===false is never a FALSE negative (the browser doesn't
 *     claim offline when a network exists), so it vetoes a probe success.
 *   • A probe FAILURE always demotes to offline, regardless of the flag — a
 *     stale `true` must never keep us "online" once the network is gone.
 *
 * @param {boolean} probeOk  did GET /api/health resolve ok?
 * @param {boolean} navigatorOnLine  navigator.onLine (the unreliable hint)
 * @returns {boolean} the connectivity state to publish
 */
export function resolveOnline(probeOk, navigatorOnLine) {
  if (!probeOk) return false
  return navigatorOnLine !== false
}
