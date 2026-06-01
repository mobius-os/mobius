/**
 * Pure connectivity-resolution logic, split out of the useOnlineStatus React
 * hook so it's unit-testable without React or a browser (the same pattern as
 * lib/appToken.js). The hook owns the effect/probe plumbing; this owns the
 * decision.
 */

// When navigator.onLine===false, how many CONSECUTIVE successful probes are
// required before we promote to online. This resolves the tension between two
// real device behaviours that present as the IDENTICAL input pair
// (probeOk=true, navigator.onLine=false):
//   • Android offline false-positive: a probe briefly succeeds while genuinely
//     offline (stale keep-alive / radio teardown). This does NOT repeat — the
//     next probe fails. So requiring 2 consecutive successes rejects it.
//   • Stale-false-after-reconnect: navigator.onLine can lag at `false` after
//     the network is actually back. Here probes succeed REPEATEDLY, so the
//     2nd consecutive success promotes us — recovery is not wedged.
// 2 is the minimum that distinguishes a one-shot transient from a real link.
export const PROMOTE_STREAK_WHEN_FLAG_OFFLINE = 2

/**
 * Resolve the next `online` value from a reachability-probe result, the
 * navigator.onLine hint, and the running count of consecutive probe successes.
 * Pure: returns BOTH the decision and the next streak count (the hook holds the
 * streak between calls). This is the temporal asymmetry that makes offline
 * cold-open fast WITHOUT regressing reconnect recovery (Codex review High #1).
 *
 *   • Probe FAILURE  → offline, streak resets to 0. (A stale `true` flag must
 *     never keep us online once the network is gone.)
 *   • Probe SUCCESS, flag NOT false → online immediately, streak counts up.
 *     (Normal online; nothing to second-guess.)
 *   • Probe SUCCESS, flag === false → the ambiguous case. Increment the streak;
 *     promote to online only once it reaches PROMOTE_STREAK_WHEN_FLAG_OFFLINE.
 *     A single Android false-positive (streak 1) stays offline; a genuine
 *     reconnect (repeated successes) promotes on the 2nd.
 *
 * @param {boolean} probeOk  did GET /api/health resolve ok?
 * @param {boolean} navigatorOnLine  navigator.onLine (the unreliable hint)
 * @param {number} successStreak  consecutive successful probes so far (caller-held)
 * @returns {{ online: boolean, successStreak: number }}
 */
export function resolveOnline(probeOk, navigatorOnLine, successStreak = 0) {
  if (!probeOk) return { online: false, successStreak: 0 }
  const nextStreak = successStreak + 1
  if (navigatorOnLine !== false) {
    return { online: true, successStreak: nextStreak }
  }
  // Flag says offline but the probe succeeded — promote only on a streak.
  return {
    online: nextStreak >= PROMOTE_STREAK_WHEN_FLAG_OFFLINE,
    successStreak: nextStreak,
  }
}
