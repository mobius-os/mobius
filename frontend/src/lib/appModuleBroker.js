import { moduleVersionKey } from './appVersion.js'

export const APP_MODULE_MAX_BYTES = 8 * 1024 * 1024

export class AppModuleBrokerError extends Error {
  constructor(message, { code = 'module-load-failed', status = null } = {}) {
    super(message)
    this.name = 'AppModuleBrokerError'
    this.code = code
    this.status = status
  }
}

export function appModuleRequestUrl(
  baseUrl,
  { token, frameVersion, retry = 0 } = {},
) {
  const url = new URL(baseUrl, globalThis.location?.origin || 'http://localhost')
  url.searchParams.set('token', String(token || ''))
  url.searchParams.set('v', moduleVersionKey(frameVersion))
  if (retry > 0) url.searchParams.set('_', String(retry))
  return url.href
}

/**
 * Fetch compiled app code from the controlled shell document.
 *
 * Sandboxed app frames have an opaque effective origin and therefore are not
 * controlled by the shell's service worker. The parent is controlled, so this
 * fetch can read the versioned CacheStorage entry while offline and transfer
 * the authenticated module bytes into the frame without weakening its sandbox.
 */
export async function fetchAppModuleBytes({
  baseUrl,
  token,
  frameVersion,
  retry = 0,
  fetchImpl = globalThis.fetch,
  maxBytes = APP_MODULE_MAX_BYTES,
}) {
  if (!token) {
    throw new AppModuleBrokerError('No app token is available.', {
      code: 'missing-token',
    })
  }
  if (typeof fetchImpl !== 'function') {
    throw new AppModuleBrokerError('Module loading is unavailable.', {
      code: 'network',
    })
  }

  const url = appModuleRequestUrl(baseUrl, { token, frameVersion, retry })
  let response
  try {
    response = await fetchImpl(url, { credentials: 'same-origin' })
  } catch {
    throw new AppModuleBrokerError('The app module could not be reached.', {
      code: 'network',
    })
  }

  if (response.status === 401 || response.status === 403) {
    throw new AppModuleBrokerError('The app token expired.', {
      code: 'token-expired',
      status: response.status,
    })
  }
  if (!response.ok) {
    throw new AppModuleBrokerError('The app module request failed.', {
      code: 'http',
      status: response.status,
    })
  }

  const declaredBytes = Number(response.headers.get('content-length'))
  if (Number.isFinite(declaredBytes) && declaredBytes > maxBytes) {
    throw new AppModuleBrokerError('The compiled app module is too large.', {
      code: 'too-large',
      status: response.status,
    })
  }
  let bytes
  try {
    bytes = await response.arrayBuffer()
  } catch {
    throw new AppModuleBrokerError('The app module download was interrupted.', {
      code: 'network',
      status: response.status,
    })
  }
  if (bytes.byteLength > maxBytes) {
    throw new AppModuleBrokerError('The compiled app module is too large.', {
      code: 'too-large',
      status: response.status,
    })
  }
  return bytes
}
