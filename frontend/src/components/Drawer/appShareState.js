/* Pure Smart Share state: published apps expose an install URL; local apps
   route through Contribute, or the App Store when Contribute is absent. */

const CONTRIBUTE_SLUG = 'contribute'
const APP_STORE_SLUG = 'app-store'

function appBySlug(apps, slug) {
  return (apps || []).find(app => app?.slug === slug) || null
}

// Store-installed apps are already discoverable in App Store, so the drawer
// only offers Share for locally-built apps. A local app stays eligible after
// the owner attaches a public share manifest.
export function isDrawerAppShareEligible(app) {
  return !app?.manifest_url
}

export function appInstallManifestUrl(app) {
  const value = app?.share_manifest_url || app?.manifest_url
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

export function appShareState(app, apps) {
  const installUrl = appInstallManifestUrl(app)
  if (installUrl) return { kind: 'published', installUrl }

  const contributeApp = appBySlug(apps, CONTRIBUTE_SLUG)
  if (contributeApp) {
    return { kind: 'open-contribute', targetApp: contributeApp }
  }

  const appStoreApp = appBySlug(apps, APP_STORE_SLUG)
  if (appStoreApp) {
    return { kind: 'install-contribute', targetApp: appStoreApp }
  }

  return { kind: 'unavailable', targetApp: null }
}

export function appInstallShareText(app, installUrl) {
  return [
    `Install ${app?.name || 'this app'} in Möbius:`,
    installUrl,
    '',
    'Open App Store → From URL, paste this link, then review and install.',
  ].join('\n')
}

export function appNativeSharePayload(app, installUrl) {
  const name = app?.name || 'Möbius app'
  return {
    title: name,
    text: `Install ${name} in Möbius. Open App Store → From URL.`,
    url: installUrl,
  }
}
