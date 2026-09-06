export const DEVICE_STORAGE = 'device.storage'

const KEY_RE = /^[A-Za-z0-9._:-]{1,128}$/
const DEFAULT_MAX_BYTES = 64 * 1024

function capabilityError(name, message, code) {
  const error = new Error(message)
  error.name = name
  error.code = code
  return error
}

function browserStorage(explicit) {
  if (explicit !== undefined) return explicit
  try { return globalThis.localStorage || null } catch { return null }
}

function storageNamespace(identity) {
  if (!identity?.appId || !identity?.appInstanceId) {
    throw capabilityError(
      'NotAllowedError',
      'Device storage is unavailable until this app session is verified.',
      'denied',
    )
  }
  return `mobius:device-storage:v1:${encodeURIComponent(identity.appId)}:${encodeURIComponent(identity.appInstanceId)}`
}

function validKey(value) {
  if (typeof value !== 'string' || !KEY_RE.test(value)) {
    throw new TypeError('Device storage keys must be 1–128 safe characters.')
  }
  return value
}

function readRecord(storage, namespace) {
  let raw
  try {
    raw = storage.getItem(namespace)
  } catch {
    throw capabilityError(
      'NotSupportedError', 'This browser does not allow device storage.', 'unavailable',
    )
  }
  if (!raw) return Object.create(null)
  try {
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return Object.create(null)
    }
    return Object.assign(Object.create(null), parsed)
  } catch {
    return Object.create(null)
  }
}

function isJsonValue(value, seen = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return true
  if (typeof value === 'number') return Number.isFinite(value)
  if (!value || typeof value !== 'object' || seen.has(value)) return false
  const prototype = Object.getPrototypeOf(value)
  if (!Array.isArray(value) && prototype !== Object.prototype && prototype !== null) return false
  seen.add(value)
  const valid = Array.isArray(value)
    ? value.every((item) => isJsonValue(item, seen))
    : Object.keys(value).every((key) => isJsonValue(value[key], seen))
  seen.delete(value)
  return valid
}

function encodedBytes(value) {
  if (!isJsonValue(value)) {
    throw new TypeError('Device storage accepts JSON values only.')
  }
  return new TextEncoder().encode(JSON.stringify(value)).byteLength
}

function writeRecord(storage, namespace, record, maxBytes) {
  let serialized
  try {
    serialized = JSON.stringify(record)
  } catch {
    throw new TypeError('Device storage accepts JSON values only.')
  }
  if (new TextEncoder().encode(serialized).byteLength > maxBytes) {
    throw capabilityError(
      'QuotaExceededError',
      `Device storage is limited to ${maxBytes} bytes for this app.`,
      'limit_exceeded',
    )
  }
  try {
    storage.setItem(namespace, serialized)
  } catch {
    throw capabilityError(
      'QuotaExceededError', 'This browser could not save the device value.', 'limit_exceeded',
    )
  }
}

export function createDeviceStorageProvider({
  appId,
  getIdentity,
  storage,
} = {}) {
  return {
    version: 1,
    async open({ input, declaration, channel }) {
      const targetStorage = browserStorage(storage)
      if (!targetStorage) {
        throw capabilityError(
          'NotSupportedError', 'This browser does not provide device storage.', 'unavailable',
        )
      }
      const identity = typeof getIdentity === 'function' ? getIdentity() : null
      if (String(identity?.appId || '') !== String(appId || '')) {
        throw capabilityError(
          'NotAllowedError', 'Device storage is not bound to this app session.', 'denied',
        )
      }
      const namespace = storageNamespace(identity)
      const maxBytes = Math.max(
        1,
        Number(declaration?.limits?.max_bytes) || DEFAULT_MAX_BYTES,
      )
      const allowed = new Set(['get', 'set', 'remove', 'list'])
      const operation = input?.operation
      if (!allowed.has(operation)) {
        throw new TypeError('Unknown device storage operation.')
      }
      const record = readRecord(targetStorage, namespace)
      if (operation === 'list') {
        channel.result(Object.keys(record).sort())
        return {}
      }
      const key = validKey(input?.key)
      if (operation === 'get') {
        channel.result(Object.hasOwn(record, key) ? record[key] : null)
        return {}
      }
      if (operation === 'remove') {
        delete record[key]
        writeRecord(targetStorage, namespace, record, maxBytes)
        channel.result({ removed: true })
        return {}
      }
      if (encodedBytes(input?.value) > maxBytes) {
        throw capabilityError(
          'QuotaExceededError',
          `Device storage is limited to ${maxBytes} bytes for this app.`,
          'limit_exceeded',
        )
      }
      record[key] = input.value
      writeRecord(targetStorage, namespace, record, maxBytes)
      channel.result({ saved: true })
      return {}
    },
  }
}

export function purgeDeviceStorage(appId, storage) {
  const targetStorage = browserStorage(storage)
  if (!targetStorage) return false
  const prefix = `mobius:device-storage:v1:${encodeURIComponent(String(appId))}:`
  try {
    const doomed = []
    for (let index = 0; index < targetStorage.length; index += 1) {
      const key = targetStorage.key(index)
      if (key?.startsWith(prefix)) doomed.push(key)
    }
    for (const key of doomed) targetStorage.removeItem(key)
    return true
  } catch {
    return false
  }
}
