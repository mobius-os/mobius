/**
 * Unit tests for lib/onlineStatus.js — the HYSTERETIC, asymmetric connectivity
 * verdict: fast recovery to online, debounced demotion to offline. This is what
 * makes offline cold-open fast WITHOUT regressing reconnect recovery AND
 * WITHOUT flapping to a spurious offline banner on one slow probe.
 *
 *   cd frontend && node --test src/lib/__tests__/onlineStatus.test.js
 *
 * resolveOnline(probeOk, navigatorOnLine, state)
 *   state  = {successStreak, failureStreak, online}  (caller-held)
 *   returns = {online, successStreak, failureStreak}
 *
 * Two streaks gate the two ambiguous cases:
 *   • probeOk=true,  navigator.onLine=false → PROMOTE streak (Android offline
 *     false-positive vs stale-false-after-reconnect).
 *   • probeOk=false, navigator.onLine!=false → DEMOTE streak (one slow/aborted
 *     probe vs a real loss of connectivity).
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  resolveOnline,
  PROMOTE_STREAK_WHEN_FLAG_OFFLINE,
  DEMOTE_STREAK_WHEN_FLAG_ONLINE,
} from '../onlineStatus.js'

test('probe success + navigator online → online immediately', () => {
  assert.deepEqual(
    resolveOnline(true, true, { successStreak: 0, failureStreak: 0, online: true }),
    { online: true, successStreak: 1, failureStreak: 0 },
  )
})

test('probe success clears a failure streak (fast recovery)', () => {
  // We were online, one probe failed (failureStreak 1, still online). The next
  // probe succeeds → back to a clean online with the failure streak reset.
  assert.deepEqual(
    resolveOnline(true, true, { successStreak: 0, failureStreak: 1, online: true }),
    { online: true, successStreak: 1, failureStreak: 0 },
  )
})

test('FAST RECOVERY: a SINGLE success clears an offline state', () => {
  // Demoted to offline after two failures; navigator.onLine reads online again.
  // One good probe is enough to recover — no streak required in this direction.
  assert.deepEqual(
    resolveOnline(true, true, { successStreak: 0, failureStreak: 2, online: false }),
    { online: true, successStreak: 1, failureStreak: 0 },
  )
})

test('DEBOUNCE: one failed probe while navigator.onLine!==false does NOT demote', () => {
  // The bug: a single slow/aborted /api/health probe used to flip the banner to
  // offline. Now it must hold the prior `online` until a second failure.
  assert.deepEqual(
    resolveOnline(false, true, { successStreak: 0, failureStreak: 0, online: true }),
    { online: true, successStreak: 0, failureStreak: 1 },
  )
})

test('DEBOUNCE: the SECOND consecutive failure demotes to offline', () => {
  // Thread the state forward across two failures, the real device sequence.
  let r = resolveOnline(false, true, { successStreak: 0, failureStreak: 0, online: true })
  assert.deepEqual(r, { online: true, successStreak: 0, failureStreak: 1 }, 'first failure: still online')
  r = resolveOnline(false, true, r)
  assert.deepEqual(r, { online: false, successStreak: 0, failureStreak: 2 }, 'second failure: offline')
})

test('a success between failures resets the failure streak (transient cannot accumulate)', () => {
  let r = resolveOnline(false, true, { successStreak: 0, failureStreak: 0, online: true }) // fail → streak 1
  assert.equal(r.failureStreak, 1)
  r = resolveOnline(true, true, r)  // success → resets, stays online
  assert.deepEqual(r, { online: true, successStreak: 1, failureStreak: 0 })
  r = resolveOnline(false, true, r) // fail again → streak 1, still online (not demoted)
  assert.deepEqual(r, { online: true, successStreak: 0, failureStreak: 1 })
})

test('probe failure WITH navigator.onLine===false → offline immediately (OS confirms)', () => {
  // No debounce when the OS itself says offline — there is nothing ambiguous.
  assert.deepEqual(
    resolveOnline(false, false, { successStreak: 0, failureStreak: 0, online: true }),
    { online: false, successStreak: 0, failureStreak: 1 },
  )
  assert.deepEqual(
    resolveOnline(false, false, { successStreak: 5, failureStreak: 0, online: true }),
    { online: false, successStreak: 0, failureStreak: 1 },
  )
})

test('device anomaly: ONE probe success while navigator.onLine===false stays OFFLINE', () => {
  // The literal device log line. A single false-positive must not promote.
  assert.deepEqual(
    resolveOnline(true, false, { successStreak: 0, failureStreak: 0, online: false }),
    { online: false, successStreak: 1, failureStreak: 0 },
  )
})

test('reconnect recovery: CONSECUTIVE successes while flag stale-false DO promote', () => {
  // Genuine reconnection where navigator.onLine lags at false: probes keep
  // succeeding, so the 2nd consecutive success promotes — recovery not wedged.
  let r = resolveOnline(true, false, { successStreak: 0, failureStreak: 0, online: false })
  assert.deepEqual(r, { online: false, successStreak: 1, failureStreak: 0 }, 'first success: still offline')
  r = resolveOnline(true, false, r)
  assert.deepEqual(r, { online: true, successStreak: 2, failureStreak: 0 }, 'second success: promoted')
})

test('promote-streak success holds an already-online verdict (no spurious bounce to offline)', () => {
  // navigator.onLine stale-false while we are ALREADY online: a single success
  // (streak 1, below threshold) must hold `online:true`, not bounce to offline.
  assert.deepEqual(
    resolveOnline(true, false, { successStreak: 0, failureStreak: 0, online: true }),
    { online: true, successStreak: 1, failureStreak: 0 },
  )
})

test('threshold constants are the minimum that distinguishes one-shot from sustained', () => {
  assert.equal(PROMOTE_STREAK_WHEN_FLAG_OFFLINE, 2)
  assert.equal(DEMOTE_STREAK_WHEN_FLAG_ONLINE, 2)
})

test('flag missing/undefined (non-browser) treated as not-false → promotes on success', () => {
  assert.deepEqual(
    resolveOnline(true, undefined, { successStreak: 0, failureStreak: 0, online: true }),
    { online: true, successStreak: 1, failureStreak: 0 },
  )
})
