/**
 * Unit tests for lib/onlineStatus.js — the connectivity asymmetry that fixes
 * the slow Android offline cold-open.
 *
 *   cd frontend && node --test src/lib/__tests__/onlineStatus.test.js
 *
 * The headline case (`device-anomaly`) encodes the exact on-device log line
 * that a desktop Playwright harness CANNOT reproduce: a probe that returns
 * reachable=true while navigator.onLine===false. resolveOnline must veto it.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { resolveOnline } from '../onlineStatus.js'

test('probe success + navigator online → online', () => {
  assert.equal(resolveOnline(true, true), true)
})

test('probe failure → offline, regardless of the flag', () => {
  assert.equal(resolveOnline(false, true), false)   // stale-true flag can't keep us online
  assert.equal(resolveOnline(false, false), false)
})

test('device-anomaly: probe wrongly succeeds while navigator.onLine===false → VETOED to offline', () => {
  // This is the literal device log: `[OFF] online=true: probe (navigator.onLine=false)`.
  // Pre-fix this promoted to online and stalled the app ~5s. resolveOnline vetoes it.
  assert.equal(resolveOnline(true, false), false)
})

test('navigator.onLine missing/unknown (treated as not-false) does not veto a real success', () => {
  // When the hint is undefined (non-browser / older engine), only a probe
  // result drives the decision; a success stays online.
  assert.equal(resolveOnline(true, undefined), true)
})
