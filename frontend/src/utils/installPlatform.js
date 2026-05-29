// Platform-aware install-instruction helpers.
//
// Mirrors the logic baked into `backend/app/routes/standalone.py`'s
// per-app standalone shell, but for the main Möbius shell. The two
// can't share source (standalone is Python-rendered HTML, this is an
// ES module the bundler picks up), but they MUST stay logically in
// sync — the walkthrough's "Add to home screen" step and the sub-app
// install card should agree on which arrow points where on a given
// device.
//
// UA-based, fragile-but-fine: we use this for hint copy only. If we
// guess wrong on an edge case the browser still has its own menu.
// Feature-detect when possible (matchMedia, navigator.standalone).

export function detectInstallPlatform(ua) {
  // Default to navigator.userAgent only if we're in a browser. Tests
  // and any future SSR path can pass an explicit UA without crashing
  // on undefined `navigator`. The `window.MSStream` check is the same
  // — only consult it when window exists.
  if (ua === undefined) {
    ua = typeof navigator !== 'undefined' ? (navigator.userAgent || '') : ''
  }
  const hasWindow = typeof window !== 'undefined'
  const ios = /iPad|iPhone|iPod/.test(ua) && !(hasWindow && window.MSStream)
  // Every iOS browser uses WebKit (Apple gates engine choice until
  // iOS 17.4 EU). CriOS = Chrome on iOS, FxiOS = Firefox on iOS, etc.
  const iosNonSafari = ios && /CriOS|FxiOS|EdgiOS|OPiOS|GSA/.test(ua)
  const iosSafari = ios && !iosNonSafari
  const android = /Android/.test(ua)
  const samsung = /SamsungBrowser/.test(ua)
  const edge = /\bEdg\//.test(ua)
  const firefox = /Firefox|FxiOS/.test(ua)
  // Chromium-family install-capable browsers — Chrome, Edge,
  // Samsung Internet. CriOS is excluded because Apple forces it to
  // be WebKit-engined on iOS.
  const chromium = !ios && (
    (/Chrome/.test(ua) && !/Edge\//.test(ua)) || edge || samsung
  )
  const desktop = !ios && !android
  return {
    ua,
    ios, iosSafari, iosNonSafari,
    android, chromium, edge, firefox, samsung, desktop,
    // `beforeinstallprompt` can fire here.
    bipCapable: chromium,
    // PWA install is possible at all on this platform.
    installPossible: iosSafari || chromium || (firefox && !ios),
  }
}

// Returns rendering hints for the "Add to home screen" walkthrough
// step. Each shape:
//   {
//     title:     short heading
//     body:      one or two sentences explaining the path
//     ctaLabel:  the primary-action button label
//     arrowDir:  'up' | 'down' | null — for any directional UI accent
//     unsupported: true on platforms where install is impossible
//                  (renders an alternate CTA, e.g. copy-link)
//   }
//
// The walkthrough overlay doesn't need to know about BIP — by the
// time the user reaches step 2 they're on a Möbius origin where the
// real install action is "tap your browser's menu" (Möbius can't
// reliably auto-fire prompt() at walkthrough-step-2 time, especially
// for first-time visitors who haven't built engagement yet). The
// honest copy matches that reality.
export function installCopyForPlatform(p = detectInstallPlatform()) {
  if (p.iosSafari) {
    return {
      title: 'Add Möbius to your home screen',
      body: 'Tap the Share button below (the square with the up-arrow), then choose Add to Home Screen.',
      ctaLabel: 'Got it',
      arrowDir: 'down',
    }
  }
  if (p.iosNonSafari) {
    return {
      title: 'Open Möbius in Safari',
      body: 'On iPhone and iPad, only Safari can install web apps. Copy this page’s link and open it in Safari, then tap Share → Add to Home Screen.',
      ctaLabel: 'Copy link',
      arrowDir: null,
      unsupported: true,
    }
  }
  if (p.firefox && p.android) {
    return {
      title: 'Add Möbius to your home screen',
      body: 'Tap the ⋮ menu at the top right, then choose Install.',
      ctaLabel: 'Got it',
      arrowDir: 'up',
    }
  }
  if (p.firefox && p.desktop) {
    return {
      title: 'Install isn’t supported in Firefox',
      body: 'Firefox on desktop doesn’t install web apps. Open Möbius in Chrome or Edge to add it as a desktop app, or just bookmark it.',
      ctaLabel: 'Got it',
      arrowDir: null,
      unsupported: true,
    }
  }
  if (p.chromium && p.android) {
    return {
      title: 'Add Möbius to your home screen',
      body: 'Tap the ⋮ menu in the address bar, then Install app (or Add to Home screen).',
      ctaLabel: 'Got it',
      arrowDir: 'up',
    }
  }
  if (p.chromium && p.desktop) {
    return {
      title: 'Install Möbius',
      body: 'Click the install icon on the right side of the address bar, or open the ⋮ menu and choose Install Möbius.',
      ctaLabel: 'Got it',
      arrowDir: 'up',
    }
  }
  return {
    title: 'Add Möbius to your home screen',
    body: 'Look for an Install or Add to Home Screen option in your browser’s menu (usually ⋮ or ⋯).',
    ctaLabel: 'Got it',
    arrowDir: null,
  }
}

// Attempts to copy the current URL to the clipboard. Used by the
// iOS-non-Safari path where the only escape hatch is paste-into-
// Safari. Returns true on success, false on any clipboard failure
// (typically lack of permission or the page not being foregrounded).
export async function copyOriginUrl() {
  try {
    await navigator.clipboard.writeText(window.location.href)
    return true
  } catch {
    return false
  }
}
