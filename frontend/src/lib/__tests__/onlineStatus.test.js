/**
 * Unit tests for lib/onlineStatus.js — the temporal connectivity asymmetry
 * that makes offline cold-open fast WITHOUT regressing reconnect recovery.
 *
 *   cd frontend && node --test src/lib/__tests__/onlineStatus.test.js
 *
 * resolveOnline(probeOk, navigatorOnLine, successStreak) -> {online, successStreak}.
 * The two cases that present as the IDENTICAL input pair (probeOk=true,
 * navigator.onLine=false) — an Android offline false-positive vs stale-false-
 * after-reconnect — are distinguished by a STREAK (Codex review High #1).
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { resolveOnline, PROMOTE_STREAK_WHEN_FLAG_OFFLINE } from '../onlineStatus.js'

test('probe success + navigator online → online immediately', () => {
  assert.deepEqual(resolveOnline(true, true, 0), { online: true, successStreak: 1 })
})

test('probe failure → offline, streak resets', () => {
  assert.deepEqual(resolveOnline(false, true, 5), { online: false, successStreak: 0 })
  assert.deepEqual(resolveOnline(false, false, 1), { online: false, successStreak: 0 })
})

test('device anomaly: ONE probe success while navigator.onLine===false stays OFFLINE', () => {
  // The literal device log line. A single false-positive must not promote.
  assert.deepEqual(resolveOnline(true, false, 0), { online: false, successStreak: 1 })
})

test('reconnect recovery: CONSECUTIVE successes while flag stale-false DO promote', () => {
  // Genuine reconnection where navigator.onLine lags at false: probes keep
  // succeeding, so the 2nd consecutive success promotes — recovery not wedged.
  let r = resolveOnline(true, false, 0)
  assert.deepEqual(r, { online: false, successStreak: 1 }, 'first success: still offline')
  r = resolveOnline(true, false, r.successStreak)
  assert.deepEqual(r, { online: true, successStreak: 2 }, 'second success: promoted')
})

test('a failure between successes resets the streak (transient cannot accumulate)', () => {
  let r = resolveOnline(true, false, 0)      // streak 1
  r = resolveOnline(false, false, r.successStreak)  // fail → reset
  assert.deepEqual(r, { online: false, successStreak: 0 })
  r = resolveOnline(true, false, r.successStreak)   // streak 1 again, still offline
  assert.deepEqual(r, { online: false, successStreak: 1 })
})

test('threshold constant is the minimum that distinguishes one-shot from sustained', () => {
  assert.equal(PROMOTE_STREAK_WHEN_FLAG_OFFLINE, 2)
})

test('flag missing/undefined (non-browser) treated as not-false → promotes on success', () => {
  assert.deepEqual(resolveOnline(true, undefined, 0), { online: true, successStreak: 1 })
})
