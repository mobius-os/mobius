/**
 * Unit tests for platform detection and manual install instructions.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  androidBrowserIntentHref,
  detectInstallPlatform,
  installCopyForPlatform,
  isStandaloneDisplay,
} from '../installPlatform.js'

const UA = {
  iosSafari: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1',
  iosChrome: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/120.0.6099.119 Mobile/15E148 Safari/604.1',
  iosFirefox: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/121.0 Mobile/15E148 Safari/605.1.15',
  ipadDesktop: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
  androidChrome: 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.144 Mobile Safari/537.36',
  androidFirefox: 'Mozilla/5.0 (Android 14; Mobile; rv:121.0) Gecko/121.0 Firefox/121.0',
  androidSamsung: 'Mozilla/5.0 (Linux; Android 14; SAMSUNG SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/23.0 Chrome/115.0.0.0 Mobile Safari/537.36',
  desktopChrome: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
  desktopEdge: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',
  windowsFirefox: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0',
  linuxFirefox: 'Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0',
  macSafari: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15',
}

test('iOS Safari and third-party browsers are all install-capable', () => {
  const safari = detectInstallPlatform(UA.iosSafari)
  const chrome = detectInstallPlatform(UA.iosChrome)
  const firefox = detectInstallPlatform(UA.iosFirefox)

  assert.equal(safari.iosSafari, true)
  assert.equal(safari.iosNonSafari, false)
  assert.equal(chrome.iosNonSafari, true)
  assert.equal(firefox.iosNonSafari, true)
  assert.equal(safari.installPossible, true)
  assert.equal(chrome.installPossible, true)
  assert.equal(firefox.installPossible, true)
})

test('iPadOS desktop UA is not mistaken for desktop Safari', () => {
  const platform = detectInstallPlatform(UA.ipadDesktop, 5)
  const copy = installCopyForPlatform(platform)

  assert.equal(platform.ios, true)
  assert.equal(platform.ipad, true)
  assert.equal(platform.desktopSafari, false)
  assert.match(copy.body, /Add to Home Screen/)
})

test('Android install-capable browsers are detected without iOS overlap', () => {
  const chrome = detectInstallPlatform(UA.androidChrome)
  const firefox = detectInstallPlatform(UA.androidFirefox)
  const samsung = detectInstallPlatform(UA.androidSamsung)

  assert.equal(chrome.android, true)
  assert.equal(chrome.chromium, true)
  assert.equal(chrome.bipCapable, true)
  assert.equal(firefox.android, true)
  assert.equal(firefox.firefox, true)
  assert.equal(samsung.samsung, true)
  assert.equal(samsung.chromium, true)
})

test('Chromium desktop includes Chrome and Edge', () => {
  const chrome = detectInstallPlatform(UA.desktopChrome)
  const edge = detectInstallPlatform(UA.desktopEdge)

  assert.equal(chrome.chromium, true)
  assert.equal(chrome.desktop, true)
  assert.equal(edge.edge, true)
  assert.equal(edge.chromium, true)
})

test('Firefox on Windows exposes current web-app instructions', () => {
  const platform = detectInstallPlatform(UA.windowsFirefox)
  const copy = installCopyForPlatform(platform)

  assert.equal(platform.firefox, true)
  assert.equal(platform.windows, true)
  assert.equal(platform.installPossible, true)
  assert.equal(copy.unsupported, undefined)
  assert.match(copy.body, /web-app button/)
})

test('Firefox on Linux offers an honest cross-browser fallback', () => {
  const platform = detectInstallPlatform(UA.linuxFirefox)
  const copy = installCopyForPlatform(platform)

  assert.equal(platform.windows, false)
  assert.equal(platform.installPossible, false)
  assert.equal(copy.unsupported, true)
  assert.match(copy.body, /Chrome|Edge|Safari/)
})

test('desktop Safari offers Add to Dock', () => {
  const platform = detectInstallPlatform(UA.macSafari)
  const copy = installCopyForPlatform(platform)

  assert.equal(platform.desktopSafari, true)
  assert.equal(platform.mac, true)
  assert.match(copy.body, /Add to Dock/)
})

test('empty UA does not crash in non-browser contexts', () => {
  const platform = detectInstallPlatform('')
  assert.equal(platform.ios, false)
  assert.equal(platform.android, false)
  assert.equal(typeof installCopyForPlatform(platform).summary, 'string')
})

test('iOS instructions use the Share menu in Safari, Chrome, and Firefox', () => {
  for (const ua of [UA.iosSafari, UA.iosChrome, UA.iosFirefox]) {
    const copy = installCopyForPlatform(detectInstallPlatform(ua))
    assert.equal(copy.unsupported, undefined)
    assert.equal(copy.ctaLabel, 'Show me')
    assert.match(copy.body, /Share/)
    assert.match(copy.body, /Add to Home Screen/)
  }
})

test('Android browsers get their own menu wording', () => {
  const chrome = installCopyForPlatform(detectInstallPlatform(UA.androidChrome))
  const firefox = installCopyForPlatform(detectInstallPlatform(UA.androidFirefox))

  assert.match(chrome.body, /browser menu/)
  assert.match(firefox.body, /Firefox menu/)
})

test('desktop Chromium manual fallback names the address bar', () => {
  const copy = installCopyForPlatform(detectInstallPlatform(UA.desktopChrome))
  assert.match(copy.body, /address bar/)
})

test('standalone mode reports installation instead of browser-chrome steps', () => {
  const copy = installCopyForPlatform(
    detectInstallPlatform(UA.iosSafari),
    true,
  )
  assert.equal(copy.title, 'Möbius is installed')
  assert.match(copy.body, /already/)
})

test('custom app identity replaces Möbius throughout install guidance', () => {
  for (const ua of [
    UA.iosSafari,
    UA.androidChrome,
    UA.androidFirefox,
    UA.desktopChrome,
    UA.windowsFirefox,
    UA.linuxFirefox,
    UA.macSafari,
  ]) {
    const copy = installCopyForPlatform(detectInstallPlatform(ua), false, 'Atlas')
    const guidance = `${copy.title} ${copy.body}`
    assert.match(guidance, /Atlas/)
    assert.doesNotMatch(guidance, /Möbius/)
  }
})

// The in-app browser iOS opens when an installed PWA follows an out-of-scope
// link is where this used to go wrong: `navigator.standalone` stays true there
// even though the page is plainly not the installed app, so the install card
// congratulated people who had not installed anything yet.
test('display-mode wins over the legacy iOS standalone flag', () => {
  const inAppBrowser = {
    navigator: { standalone: true },
    matchMedia: query => ({ media: query, matches: false }),
  }
  assert.equal(isStandaloneDisplay(inAppBrowser), false)

  const installedApp = {
    navigator: { standalone: true },
    matchMedia: query => ({
      media: query,
      matches: query.includes('standalone'),
    }),
  }
  assert.equal(isStandaloneDisplay(installedApp), true)
})

test('every installed manifest display mode counts as standalone', () => {
  for (const mode of ['standalone', 'fullscreen', 'minimal-ui']) {
    let asked = ''
    const target = {
      navigator: { standalone: false },
      matchMedia: query => {
        asked = query
        return { matches: query.includes(`display-mode: ${mode}`) }
      },
    }
    assert.equal(isStandaloneDisplay(target), true, mode)
    assert.match(asked, /standalone/)
    assert.match(asked, /fullscreen/)
    assert.match(asked, /minimal-ui/)
  }
})

test('the legacy flag still answers where display-mode is unavailable', () => {
  assert.equal(isStandaloneDisplay({ navigator: { standalone: true } }), true)
  assert.equal(isStandaloneDisplay({ navigator: { standalone: false } }), false)
})

test('standalone detection never throws on hostile or absent globals', () => {
  assert.equal(isStandaloneDisplay(null), false)
  assert.equal(isStandaloneDisplay({}), false)
  assert.equal(isStandaloneDisplay({ matchMedia() { throw new Error('denied') } }), false)
})

test('Android Chromium copy warns against Create shortcut', () => {
  const copy = installCopyForPlatform(detectInstallPlatform(UA.androidChrome))
  assert.match(copy.body, /Install/)
  assert.match(copy.body, /not Create shortcut/)
})

test('androidBrowserIntentHref escapes the in-app tab with a fallback', () => {
  const href = androidBrowserIntentHref('https://mobius.example/apps/notes/?install=1')
  assert.equal(
    href,
    'intent://mobius.example/apps/notes/?install=1' +
      '#Intent;scheme=https;S.browser_fallback_url=' +
      'https%3A%2F%2Fmobius.example%2Fapps%2Fnotes%2F%3Finstall%3D1;end',
  )
  // Non-https input is left alone rather than turned into a broken intent.
  assert.equal(androidBrowserIntentHref('http://x.test/a'), 'http://x.test/a')
})
