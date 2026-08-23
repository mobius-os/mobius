// Platform-aware instructions for installing the main Möbius shell.
//
// UA detection is intentionally limited to hint copy. Native install
// availability is feature-detected separately through `beforeinstallprompt`;
// if a browser changes its menu, Möbius still leaves installation to the
// browser rather than pretending a guessed instruction is an API.

/**
 * True when THIS document is running as an installed standalone web app.
 *
 * `display-mode` is the authority. Apple's legacy `navigator.standalone`
 * predates manifests and leaks `true` into the in-app browser iOS opens when
 * an installed PWA follows a link out of its scope — a page that is plainly
 * NOT the installed app. Trusting it there made the install card announce
 * "already on your home screen" to someone who was mid-install.
 *
 * It stays as a fallback only where the media query is genuinely absent, so
 * browsers too old to answer the standard question keep their old answer.
 */
export function isStandaloneDisplay(target = typeof window !== 'undefined' ? window : null) {
  try {
    // A manifest may choose any non-browser display mode. In particular,
    // games use `fullscreen`, which must still count as an installed launch.
    const query = target?.matchMedia?.(
      '(display-mode: standalone), (display-mode: fullscreen), (display-mode: minimal-ui)',
    )
    if (query) return query.matches === true
    return target?.navigator?.standalone === true
  } catch {
    return false
  }
}

export function detectInstallPlatform(ua, maxTouchPoints) {
  if (ua === undefined) {
    ua = typeof navigator !== 'undefined' ? (navigator.userAgent || '') : ''
  }
  if (maxTouchPoints === undefined) {
    maxTouchPoints = typeof navigator !== 'undefined'
      ? (navigator.maxTouchPoints || 0)
      : 0
  }
  const hasWindow = typeof window !== 'undefined'
  // iPadOS can request a desktop UA and identify as Macintosh. Touch points
  // distinguish that mode from a real Mac for install-copy purposes.
  const ipadDesktop = /Macintosh/.test(ua) && maxTouchPoints > 1
  const ipad = /iPad/.test(ua) || ipadDesktop
  const ios = (/iPad|iPhone|iPod/.test(ua) || ipadDesktop) &&
    !(hasWindow && window.MSStream)
  const iosNonSafari = ios && /CriOS|FxiOS|EdgiOS|OPiOS|GSA/.test(ua)
  const iosSafari = ios && !iosNonSafari
  const android = /Android/.test(ua)
  const samsung = /SamsungBrowser/.test(ua)
  const edge = /\bEdg\//.test(ua)
  const firefox = /Firefox|FxiOS/.test(ua)
  const windows = /Windows/.test(ua)
  const mac = /Macintosh|Mac OS X/.test(ua) && !ios
  const desktopSafari = !ios && /Safari/.test(ua) &&
    !/Chrome|Chromium|CriOS|Edg|OPR|Firefox/.test(ua)
  const chromium = !ios && (
    (/Chrome/.test(ua) && !/Edge\//.test(ua)) || edge || samsung
  )
  const desktop = !ios && !android

  return {
    ua,
    ios, ipad, iosSafari, iosNonSafari,
    android, chromium, edge, firefox, samsung, desktop,
    desktopSafari, mac, windows,
    // `beforeinstallprompt` can fire here.
    bipCapable: chromium,
    // iOS 16.4+ allows third-party browsers to expose Add to Home Screen.
    // Firefox desktop currently supports web apps on Windows only.
    installPossible: ios || chromium || desktopSafari ||
      (firefox && (android || windows)),
  }
}

/**
 * Android intent: URI that asks the OS to open `httpsUrl` in the user's full
 * browser rather than the in-app tab the current context would use.
 *
 * From inside an installed PWA (WebAPK), an ordinary link to a page outside
 * the app's scope opens in a Custom Tab, where Chromium never offers installs
 * (`beforeinstallprompt` does not fire there). Intent resolution leaves the
 * app entirely: the target is outside this app's scope, so Android hands the
 * URL to the default browser as a real tab. `browser_fallback_url` keeps the
 * link working if nothing claims the intent.
 */
export function androidBrowserIntentHref(httpsUrl) {
  const url = new URL(httpsUrl)
  if (url.protocol !== 'https:') return httpsUrl
  const pathAndQuery = `${url.pathname}${url.search}`
  return (
    `intent://${url.host}${pathAndQuery}` +
    `#Intent;scheme=https;S.browser_fallback_url=${encodeURIComponent(httpsUrl)};end`
  )
}

// Manual fallback for browsers that do not expose `beforeinstallprompt`.
// Native prompt availability always wins in the UI; these instructions keep
// iOS, Android, and desktop useful without it.
export function installCopyForPlatform(
  p = detectInstallPlatform(),
  standaloneMode = false,
  productName = 'Möbius',
) {
  if (standaloneMode) {
    return {
      title: `${productName} is installed`,
      summary: 'It already opens as an app on this device.',
      body: 'You’re already using the installed app.',
      ctaLabel: 'Got it',
    }
  }

  if (p.ios) {
    return {
      title: `Add ${productName} to your Home Screen`,
      summary: 'Use Share, then Add to Home Screen.',
      body: 'Open your browser’s Share menu, choose Add to Home Screen, keep Open as Web App turned on, then tap Add.',
      ctaLabel: 'Show me',
    }
  }

  if (p.firefox && p.android) {
    return {
      title: `Install ${productName}`,
      summary: 'Use the Firefox menu to add it.',
      body: `Open the Firefox menu, tap Install, then add ${productName} to your Home Screen.`,
      ctaLabel: 'Show me',
    }
  }

  if (p.firefox && p.windows) {
    return {
      title: `Install ${productName}`,
      summary: 'Pin it to your Windows taskbar.',
      body: `Click the web-app button in the Firefox address bar. ${productName} will open in its own window and appear in your taskbar and Start menu.`,
      ctaLabel: 'Show me',
    }
  }

  if (p.chromium && p.android) {
    return {
      title: `Install ${productName}`,
      summary: 'Use your browser menu to add it.',
      body: 'Open the browser menu, then choose Install app. If you only see ' +
        'Add to Home screen, tap it and pick Install — not Create shortcut, ' +
        'which only makes a browser bookmark.',
      ctaLabel: 'Show me',
    }
  }

  if (p.chromium && p.desktop) {
    return {
      title: `Install ${productName}`,
      summary: 'Keep it in your dock or taskbar.',
      body: `Click the install icon in the address bar, or open the browser menu and choose Install ${productName}.`,
      ctaLabel: 'Show me',
    }
  }

  if (p.desktopSafari && p.mac) {
    return {
      title: `Add ${productName} to your Dock`,
      summary: 'Open it like a Mac app.',
      body: `In Safari, open the File menu and choose Add to Dock.`,
      ctaLabel: 'Show me',
    }
  }

  if (p.firefox && p.desktop) {
    return {
      title: `Keep ${productName} close`,
      summary: 'This Firefox version cannot install web apps.',
      body: `Open ${productName} in Chrome, Edge, or Safari to install it as an app, or bookmark this page in Firefox.`,
      ctaLabel: 'Options',
      unsupported: true,
    }
  }

  return {
    title: `Install ${productName}`,
    summary: 'Keep it one click away.',
    body: 'Look for Install, Add to Home Screen, or Add to Dock in your browser’s address bar or menu.',
    ctaLabel: 'Show me',
  }
}
