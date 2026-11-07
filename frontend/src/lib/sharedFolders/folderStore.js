/* This browser's shared folders, remembered in IndexedDB (Chrome keeps folder handles there). */

export const SHARED_FOLDERS_DB = 'mobius-shared-folders'
const DB_VERSION = 1
const STORE = 'folders'
const DEVICE_KEY = 'mobius-shared-folders-device'

function openDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(SHARED_FOLDERS_DB, DB_VERSION)
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE)) {
        request.result.createObjectStore(STORE, { keyPath: 'id' })
      }
    }
    request.onsuccess = () => {
      const db = request.result
      // Let sign-out delete the database instead of being blocked by this tab.
      db.onversionchange = () => db.close()
      resolve(db)
    }
    request.onerror = () => reject(request.error)
    request.onblocked = () => reject(new Error('Shared folders storage is busy in another tab.'))
  })
}

async function withStore(mode, action) {
  const db = await openDb()
  try {
    return await new Promise((resolve, reject) => {
      const transaction = db.transaction(STORE, mode)
      const request = action(transaction.objectStore(STORE))
      transaction.oncomplete = () => resolve(request?.result)
      transaction.onerror = () => reject(transaction.error)
      transaction.onabort = () => reject(transaction.error)
    })
  } finally {
    db.close()
  }
}

/** Saved records: { id, name, mode, access, handle?, paused, createdAt }. */
export async function loadFolderRecords() {
  const records = await withStore('readonly', store => store.getAll())
  return (records || []).sort((a, b) => a.createdAt - b.createdAt)
}

export function saveFolderRecord(record) {
  return withStore('readwrite', store => store.put(record))
}

export function deleteFolderRecord(id) {
  return withStore('readwrite', store => store.delete(id))
}

export function newFolderId() {
  const bytes = crypto.getRandomValues(new Uint8Array(12))
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
}

/** A stable id for this browser profile, so the agent can tell devices apart. */
export function deviceId() {
  try {
    const saved = localStorage.getItem(DEVICE_KEY)
    if (saved && /^[A-Za-z0-9_-]{1,64}$/.test(saved)) return saved
    const created = newFolderId()
    localStorage.setItem(DEVICE_KEY, created)
    return created
  } catch {
    return 'this-browser'
  }
}

const BROWSERS = [
  [/Edg\//, 'Edge'], [/OPR\//, 'Opera'], [/SamsungBrowser\//, 'Samsung Internet'],
  [/Firefox\/|FxiOS\//, 'Firefox'], [/Chrome\/|CriOS\//, 'Chrome'], [/Safari\//, 'Safari'],
]
const SYSTEMS = [
  [/iPhone/, 'iPhone'], [/iPad/, 'iPad'], [/Android/, 'Android'], [/Mac OS X|Macintosh/, 'Mac'],
  [/Windows/, 'Windows'], [/CrOS/, 'ChromeOS'], [/Linux/, 'Linux'],
]

/** "Brave", "Safari", "Chrome": the browser's own name. */
export function browserName(navigator = globalThis.navigator) {
  // Brave reports Chrome's user agent; only its navigator.brave tells them apart.
  if (navigator?.brave) return 'Brave'
  const userAgent = navigator?.userAgent || ''
  return BROWSERS.find(([pattern]) => pattern.test(userAgent))?.[1] || 'This browser'
}

/** "Chrome on Mac", "Brave on Android": what the agent and owner see. */
export function deviceLabel(navigator = globalThis.navigator) {
  const userAgent = navigator?.userAgent || ''
  const browser = browserName(navigator)
  const system = SYSTEMS.find(([pattern]) => pattern.test(userAgent))?.[1]
  return system ? `${browser} on ${system}` : browser
}
