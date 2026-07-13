const STORAGE_PREFIX = 'mobius:app-frame-storage:'
const TOKEN_PREFIX = 'mobius:app-token:'
const MAX_KEY_LENGTH = 256
const MAX_VALUE_LENGTH = 2 * 1024 * 1024
const SHARED_KEYS = new Set([
  'mobius:setup-complete:v1',
  'mobius:system-setup-ready:v1',
])

function storageOrNull(storage) {
  if (storage) return storage
  try { return typeof localStorage !== 'undefined' ? localStorage : null } catch { return null }
}

function appPrefix(appId) {
  return `${STORAGE_PREFIX}${encodeURIComponent(String(appId))}:`
}

function tokenKey(appId) {
  return `${TOKEN_PREFIX}${encodeURIComponent(String(appId))}`
}

export function isSafeVirtualStorageKey(key) {
  if (typeof key !== 'string' || !key || key.length > MAX_KEY_LENGTH) return false
  const normalized = key.toLowerCase()
  return normalized !== 'token' &&
    !normalized.includes('access_token') &&
    !normalized.includes('refresh_token') &&
    !normalized.includes('authorization') &&
    !normalized.startsWith(STORAGE_PREFIX) &&
    !normalized.startsWith(TOKEN_PREFIX)
}

export function isSharedVirtualStorageKey(key) {
  return SHARED_KEYS.has(key)
}

export function readAppFrameStorage(appId, storage) {
  const store = storageOrNull(storage)
  if (!store) return {}
  const snapshot = {}
  try {
    // Compatibility migration: old same-origin frames wrote app preferences
    // directly into shell localStorage. They may read those safe values once;
    // all future writes are isolated under this app's private prefix below.
    for (let i = 0; i < store.length; i += 1) {
      const key = store.key(i)
      if (!isSafeVirtualStorageKey(key)) continue
      const value = store.getItem(key)
      if (typeof value === 'string' && value.length <= MAX_VALUE_LENGTH) snapshot[key] = value
    }
    const prefix = appPrefix(appId)
    for (let i = 0; i < store.length; i += 1) {
      const physicalKey = store.key(i)
      if (!physicalKey?.startsWith(prefix)) continue
      let key
      try { key = decodeURIComponent(physicalKey.slice(prefix.length)) } catch { continue }
      if (!isSafeVirtualStorageKey(key)) continue
      const value = store.getItem(physicalKey)
      if (typeof value === 'string' && value.length <= MAX_VALUE_LENGTH) snapshot[key] = value
    }
  } catch { return {} }
  return snapshot
}

export function setAppFrameStorage(appId, key, value, storage) {
  if (!isSafeVirtualStorageKey(key) || typeof value !== 'string' || value.length > MAX_VALUE_LENGTH) return false
  const store = storageOrNull(storage)
  if (!store) return false
  try {
    const physicalKey = isSharedVirtualStorageKey(key)
      ? key
      : `${appPrefix(appId)}${encodeURIComponent(key)}`
    store.setItem(physicalKey, value)
    return true
  } catch { return false }
}

export function removeAppFrameStorage(appId, key, storage) {
  if (!isSafeVirtualStorageKey(key)) return false
  const store = storageOrNull(storage)
  if (!store) return false
  try {
    const physicalKey = isSharedVirtualStorageKey(key)
      ? key
      : `${appPrefix(appId)}${encodeURIComponent(key)}`
    store.removeItem(physicalKey)
    return true
  } catch { return false }
}

export function clearAppFrameStorage(appId, storage) {
  const store = storageOrNull(storage)
  if (!store) return
  const prefix = appPrefix(appId)
  try {
    const keys = []
    for (let i = 0; i < store.length; i += 1) {
      const key = store.key(i)
      if (key?.startsWith(prefix)) keys.push(key)
    }
    keys.forEach((key) => store.removeItem(key))
  } catch {}
}

function decodeJwtPayload(token) {
  try {
    const encoded = token.split('.')[1]
    if (!encoded) return null
    const normalized = encoded.replace(/-/g, '+').replace(/_/g, '/')
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=')
    const json = typeof atob === 'function'
      ? decodeURIComponent(Array.from(atob(padded), (c) => `%${c.charCodeAt(0).toString(16).padStart(2, '0')}`).join(''))
      : Buffer.from(padded, 'base64').toString('utf8')
    return JSON.parse(json)
  } catch { return null }
}

export function readCachedAppToken(appId, storage, now = Date.now()) {
  const store = storageOrNull(storage)
  if (!store) return undefined
  try {
    const token = store.getItem(tokenKey(appId))
    const claims = token && decodeJwtPayload(token)
    const valid = claims?.scope === 'app' &&
      String(claims.app_id) === String(appId) &&
      Number.isFinite(Number(claims.exp)) &&
      Number(claims.exp) * 1000 > now + 30_000
    if (valid) return token
    store.removeItem(tokenKey(appId))
  } catch {}
  return undefined
}

export function cacheAppToken(appId, token, storage) {
  const store = storageOrNull(storage)
  const claims = token && decodeJwtPayload(token)
  if (!store || claims?.scope !== 'app' || String(claims.app_id) !== String(appId)) return false
  try { store.setItem(tokenKey(appId), token); return true } catch { return false }
}

export function clearCachedAppTokens(storage) {
  const store = storageOrNull(storage)
  if (!store) return
  try {
    const keys = []
    for (let i = 0; i < store.length; i += 1) {
      const key = store.key(i)
      if (key?.startsWith(TOKEN_PREFIX)) keys.push(key)
    }
    keys.forEach((key) => store.removeItem(key))
  } catch {}
}

export const _storagePrefixes = { STORAGE_PREFIX, TOKEN_PREFIX }
