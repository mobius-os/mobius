/**
 * Pure connectivity-resolution logic, split out of the useOnlineStatus React
 * hook so it's unit-testable without React or a browser (the same pattern as
 * lib/appToken.js). The hook owns the effect/probe plumbing; this owns the
 * decision.
 *
 * The verdict is HYSTERETIC and ASYMMETRIC, because the two failure modes are
 * not equally costly. A spurious "offline" banner (the user is online, one
 * probe was slow on a cold radio) is annoying and frequent; a slightly slow
 * "offline" (the user really did lose the network and we take one extra probe
 * to admit it) is benign. So we recover to online FAST (a single good probe
 * clears offline) but demote to offline SLOWLY (we require a streak of failures
 * before flipping online -> offline, unless the OS itself confirms offline).
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

// Symmetric to the promote streak, but for the demote direction: when the OS
// still thinks we're online (navigator.onLine !== false) yet a probe FAILS, how
// many CONSECUTIVE failures are required before we flip online -> offline. This
// debounces the spurious offline banner: a single slow/aborted /api/health
// probe (a cold radio that answers just past PROBE_TIMEOUT_MS, a one-off DNS
// hiccup) no longer demotes — only a sustained inability to reach the server
// does. 2 is the minimum that distinguishes a one-shot slow probe from a real
// loss of connectivity.
//
// This does NOT slow down a GENUINE offline transition the user notices: the
// browser's window `offline` event flips the indicator immediately (the hook
// publishes false on that event directly), and when navigator.onLine itself
// reads `false` a failing probe demotes at once (see below). The streak only
// gates the ambiguous "OS says online, probe failed" case, which is exactly
// where the spurious flap lives.
export const DEMOTE_STREAK_WHEN_FLAG_ONLINE = 2

/**
 * Resolve the next connectivity verdict from a reachability-probe result, the
 * navigator.onLine hint, and the caller-held connectivity state. Pure: returns
 * the decision AND the next state (the hook holds the state between calls). The
 * temporal asymmetry — fast recovery, debounced demotion — is what makes the
 * offline cold-open fast WITHOUT regressing reconnect recovery and WITHOUT
 * flapping to a spurious offline banner on one slow probe.
 *
 *   • Probe SUCCESS, flag NOT false → online immediately; successStreak counts
 *     up, failureStreak resets. (Normal online; nothing to second-guess.)
 *   • Probe SUCCESS, flag === false → the ambiguous promote case. Increment
 *     successStreak; promote to online only once it reaches
 *     PROMOTE_STREAK_WHEN_FLAG_OFFLINE. Below the streak, keep the PRIOR online
 *     value — a single Android false-positive must not flip an offline UI to
 *     online, and must not bounce an already-online UI back to offline either.
 *   • Probe FAILURE, flag === false → offline immediately; the OS itself
 *     confirms the loss, so there's nothing to debounce. failureStreak counts
 *     up (informational), successStreak resets.
 *   • Probe FAILURE, flag NOT false → the ambiguous demote case (OS says
 *     online, but the server is unreachable). Increment failureStreak; demote
 *     to offline only once it reaches DEMOTE_STREAK_WHEN_FLAG_ONLINE. Below the
 *     streak, keep the PRIOR online value — one slow/aborted probe does not
 *     flip an online UI to offline.
 *
 * @param {boolean} probeOk  did GET /api/health resolve ok?
 * @param {boolean} navigatorOnLine  navigator.onLine (the unreliable hint)
 * @param {{successStreak?: number, failureStreak?: number, online?: boolean}} [state]
 *   caller-held connectivity state. `online` is the CURRENT verdict, threaded
 *   so a sub-threshold probe holds the prior value instead of guessing. A bare
 *   number is also accepted for back-compat (treated as successStreak with the
 *   other fields at their defaults).
 * @returns {{ online: boolean, successStreak: number, failureStreak: number }}
 */
export function resolveOnline(probeOk, navigatorOnLine, state = {}) {
  // Back-compat: an earlier signature passed the success streak as a bare
  // number. Destructuring a number yields the defaults, so coerce explicitly.
  const s = typeof state === 'number' ? { successStreak: state } : (state || {})
  const successStreak = s.successStreak ?? 0
  const failureStreak = s.failureStreak ?? 0
  const online = s.online ?? true

  if (probeOk) {
    const nextSuccess = successStreak + 1
    if (navigatorOnLine !== false) {
      return { online: true, successStreak: nextSuccess, failureStreak: 0 }
    }
    // Flag says offline but the probe succeeded — promote only on a streak;
    // otherwise hold the prior verdict (don't bounce in either direction).
    const promoted = nextSuccess >= PROMOTE_STREAK_WHEN_FLAG_OFFLINE
    return {
      online: promoted ? true : online,
      successStreak: nextSuccess,
      failureStreak: 0,
    }
  }

  // Probe FAILURE.
  const nextFailure = failureStreak + 1
  if (navigatorOnLine === false) {
    // The OS confirms offline too — nothing ambiguous, flip immediately.
    return { online: false, successStreak: 0, failureStreak: nextFailure }
  }
  // Flag says online but the probe failed — demote only on a streak; otherwise
  // hold the prior verdict so one slow/aborted probe can't flap the banner.
  const demoted = nextFailure >= DEMOTE_STREAK_WHEN_FLAG_ONLINE
  return {
    online: demoted ? false : online,
    successStreak: 0,
    failureStreak: nextFailure,
  }
}
