const FALLBACK_APP_NAME = 'this app'
const REASSURANCE = 'The previous version is still running.'
const APP_STORE_MANIFEST_PREFIX = 'https://raw.githubusercontent.com/mobius-os/app-store/'

function compact(text) {
  return String(text || '').replace(/\s+/g, ' ').trim()
}

function appName(event) {
  return compact(event?.appName || event?.name || '') || FALLBACK_APP_NAME
}

export function appUpdateStaleMessage(event) {
  return `The pending update for ${appName(event)} changed upstream. `
    + `Review the latest update and start again. ${REASSURANCE}`
}

export function findAppStoreApp(apps) {
  let nameFallback = null
  for (const app of Array.isArray(apps) ? apps : []) {
    if (String(app?.manifest_url || '').startsWith(APP_STORE_MANIFEST_PREFIX)) {
      return app
    }
    if (nameFallback == null && app?.name === 'App Store') nameFallback = app
  }
  return nameFallback
}
