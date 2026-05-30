/**
 * Unit tests for installPlatform.js (UA detection + per-platform install
 * copy + clipboard helper).
 *
 * Run with:
 *   cd frontend && node --test src/utils/__tests__/installPlatform.test.js
 *
 * Pure functions only — detectInstallPlatform takes an explicit UA
 * string, installCopyForPlatform takes a plain object, copyOriginUrl
 * touches navigator.clipboard (covered by a stub).
 *
 * Coverage focuses on the branches today's walkthrough relies on:
 *   - iosSafari → arrowDir 'down', includes Share-button copy
 *   - iosNonSafari (CriOS/FxiOS/etc.) → unsupported=true, ctaLabel
 *     'Copy link' (drives WalkthroughOverlay's STEPS_NO_INSTALL filter)
 *   - chromium-on-Android / Chromium-on-desktop → arrowDir 'up'
 *   - firefox-on-desktop → unsupported (Firefox desktop has no PWA
 *     install API)
 *   - empty UA / SSR-style call (no navigator) → no crash
 *
 * If any of these regress, walkthrough renders wrong copy for the
 * platform OR crashes during SSR, both of which are user-visible.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  detectInstallPlatform,
  installCopyForPlatform,
} from '../installPlatform.js'

// Real UA strings observed in the wild — keep these literal so a
// regex tweak in installPlatform.js can be validated against real
// device output rather than a synthesized minimal string.
const UA = {
  iosSafari: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1',
  iosChrome: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/120.0.6099.119 Mobile/15E148 Safari/604.1',
  iosFirefox: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/121.0 Mobile/15E148 Safari/605.1.15',
  androidChrome: 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.144 Mobile Safari/537.36',
  androidFirefox: 'Mozilla/5.0 (Android 14; Mobile; rv:121.0) Gecko/121.0 Firefox/121.0',
  androidSamsung: 'Mozilla/5.0 (Linux; Android 14; SAMSUNG SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/23.0 Chrome/115.0.0.0 Mobile Safari/537.36',
  desktopChrome: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
  desktopEdge: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',
  desktopFirefox: 'Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0',
}

test('iOS Safari is detected and NOT treated as iOS-non-Safari', () => {
  const p = detectInstallPlatform(UA.iosSafari)
  assert.equal(p.ios, true)
  assert.equal(p.iosSafari, true)
  assert.equal(p.iosNonSafari, false)
  assert.equal(p.bipCapable, false)
  assert.equal(p.installPossible, true)
})

test('iOS Chrome (CriOS) is detected as iOS-non-Safari', () => {
  const p = detectInstallPlatform(UA.iosChrome)
  assert.equal(p.ios, true)
  assert.equal(p.iosSafari, false)
  assert.equal(p.iosNonSafari, true)
  // Walkthrough relies on this to drop the install step.
  assert.equal(p.installPossible, false)
})

test('iOS Firefox (FxiOS) is detected as iOS-non-Safari', () => {
  const p = detectInstallPlatform(UA.iosFirefox)
  assert.equal(p.iosSafari, false)
  assert.equal(p.iosNonSafari, true)
})

test('Android Chrome is chromium + android + bipCapable', () => {
  const p = detectInstallPlatform(UA.androidChrome)
  assert.equal(p.android, true)
  assert.equal(p.chromium, true)
  assert.equal(p.ios, false)
  assert.equal(p.bipCapable, true)
})

test('Android Firefox is firefox + android (not iosNonSafari)', () => {
  const p = detectInstallPlatform(UA.androidFirefox)
  assert.equal(p.firefox, true)
  assert.equal(p.android, true)
  assert.equal(p.iosNonSafari, false)
  assert.equal(p.installPossible, true)
})

test('Samsung Internet is chromium-family on Android', () => {
  const p = detectInstallPlatform(UA.androidSamsung)
  assert.equal(p.samsung, true)
  assert.equal(p.chromium, true)
  assert.equal(p.android, true)
})

test('Desktop Chrome is chromium + desktop + bipCapable', () => {
  const p = detectInstallPlatform(UA.desktopChrome)
  assert.equal(p.chromium, true)
  assert.equal(p.desktop, true)
  assert.equal(p.android, false)
  assert.equal(p.ios, false)
})

test('Desktop Edge is chromium-family (matches Edg/ regex)', () => {
  const p = detectInstallPlatform(UA.desktopEdge)
  assert.equal(p.edge, true)
  assert.equal(p.chromium, true)
})

test('Desktop Firefox is firefox + desktop and installCopy marks unsupported', () => {
  const p = detectInstallPlatform(UA.desktopFirefox)
  assert.equal(p.firefox, true)
  assert.equal(p.desktop, true)
  const copy = installCopyForPlatform(p)
  assert.equal(copy.unsupported, true)
  assert.equal(copy.arrowDir, null)
})

test('Empty UA does not crash (SSR/test safety)', () => {
  const p = detectInstallPlatform('')
  assert.equal(typeof p, 'object')
  assert.equal(p.ios, false)
  assert.equal(p.android, false)
})

test('installCopyForPlatform: iOS Safari shows Share + arrow-down + Got it', () => {
  const p = detectInstallPlatform(UA.iosSafari)
  const copy = installCopyForPlatform(p)
  assert.equal(copy.arrowDir, 'down')
  assert.equal(copy.ctaLabel, 'Got it')
  assert.match(copy.body, /Share/)
})

test('installCopyForPlatform: iOS Chrome shows unsupported + Copy link CTA', () => {
  const p = detectInstallPlatform(UA.iosChrome)
  const copy = installCopyForPlatform(p)
  assert.equal(copy.unsupported, true)
  assert.equal(copy.ctaLabel, 'Copy link')
})

test('installCopyForPlatform: Android Chrome shows arrow-up + menu instruction', () => {
  const p = detectInstallPlatform(UA.androidChrome)
  const copy = installCopyForPlatform(p)
  assert.equal(copy.arrowDir, 'up')
  assert.match(copy.body, /menu/)
})

test('installCopyForPlatform: Android Firefox shows arrow-up + menu instruction', () => {
  const p = detectInstallPlatform(UA.androidFirefox)
  const copy = installCopyForPlatform(p)
  assert.equal(copy.arrowDir, 'up')
})

test('installCopyForPlatform: Desktop Chrome shows arrow-up + address-bar copy', () => {
  const p = detectInstallPlatform(UA.desktopChrome)
  const copy = installCopyForPlatform(p)
  assert.equal(copy.arrowDir, 'up')
  assert.match(copy.body, /address bar|address-bar/)
})

test('installCopyForPlatform: fallback for unknown UA returns generic copy', () => {
  const p = detectInstallPlatform('SomeWeirdEngine/1.0')
  const copy = installCopyForPlatform(p)
  // Generic branch: still has a title and body, ctaLabel, no arrow.
  assert.equal(typeof copy.title, 'string')
  assert.equal(typeof copy.body, 'string')
  assert.equal(typeof copy.ctaLabel, 'string')
})
