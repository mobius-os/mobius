// An app's navigation location: a small JSON value the app reports with
// window.mobius.nav.setLocation() and receives back in its next frame's init.
// The frame runtime and the shell both validate it here, so the shell stores
// only bounded JSON text and never evaluates it.

export const APP_NAV_LOCATION_MAX_BYTES = 4096

function byteLength(text) {
  return new TextEncoder().encode(text).length
}

/** Encode a location for the wire. null/undefined clears it. Throws on misuse. */
export function encodeNavLocation(value) {
  if (value === undefined || value === null) return null
  let text
  try {
    text = JSON.stringify(value)
  } catch (error) {
    throw new TypeError(
      `window.mobius.nav.setLocation: the location must be JSON-serializable (${error?.message || error})`,
    )
  }
  if (typeof text !== 'string' || text === 'null') {
    throw new TypeError('window.mobius.nav.setLocation: the location must be JSON-serializable')
  }
  const bytes = byteLength(text)
  if (bytes > APP_NAV_LOCATION_MAX_BYTES) {
    throw new RangeError(
      `window.mobius.nav.setLocation: the location is ${bytes} bytes as JSON; `
      + `the limit is ${APP_NAV_LOCATION_MAX_BYTES}. Keep ids and view names `
      + 'here and larger state in window.mobius.storage.',
    )
  }
  return text
}

/** Return `text` when it is bounded, parseable location JSON, else null. */
export function validNavLocationText(text) {
  if (typeof text !== 'string' || text === 'null') return null
  if (text.length > APP_NAV_LOCATION_MAX_BYTES) return null
  if (byteLength(text) > APP_NAV_LOCATION_MAX_BYTES) return null
  try {
    JSON.parse(text)
  } catch {
    return null
  }
  return text
}
