/* In-tab state for shared folders: saved records, live access, and the snapshot Settings shows. */

const GLOBAL_KEY = Symbol.for('mobius.shared-folders-host.v1')

function initialState() {
  // Inside the Möbius phone app, its own folder picker is used even where the
  // WebView has showDirectoryPicker (Android), so both apps behave the same.
  const canPickNative = typeof globalThis.MobiusNative?.pickFolder === 'function'
  return {
    // loading | ready | restart (server lacks the feature) | unavailable (guest or no support)
    status: 'loading',
    canPickNative,
    // Older app builds only read folders.
    canEditNative: canPickNative && typeof globalThis.MobiusNative?.writeFolderFile === 'function',
    canRemember: canPickNative
      || (typeof globalThis.showDirectoryPicker === 'function' && globalThis.isSecureContext !== false),
    canPickForVisit: typeof document !== 'undefined'
      && 'webkitdirectory' in document.createElement('input'),
    folders: [],
    activity: null,
  }
}

/**
 * One store per page, kept on globalThis so a hot shell reload keeps the
 * folders this tab already holds (a one-visit folder cannot be recovered).
 */
export function hostStore() {
  if (!globalThis[GLOBAL_KEY]) {
    globalThis[GLOBAL_KEY] = {
      state: initialState(),
      listeners: new Set(),
      records: new Map(),
      access: new Map(),
      permissions: new Map(),
      subscriptionId: null,
      sequence: 0,
      started: false,
    }
  }
  return globalThis[GLOBAL_KEY]
}

export function getSharedFoldersState() {
  return hostStore().state
}

export function subscribeSharedFolders(listener) {
  const store = hostStore()
  store.listeners.add(listener)
  return () => store.listeners.delete(listener)
}

export function publish(changes) {
  const store = hostStore()
  store.state = { ...store.state, ...changes }
  for (const listener of store.listeners) listener()
}

/** What a folder can do right now in this tab. */
export function folderState(record) {
  const store = hostStore()
  if (record.paused) return 'paused'
  if (record.access === 'visit') return store.access.has(record.id) ? 'ready' : 'needs_pick'
  return store.permissions.get(record.id) === 'granted' ? 'ready' : 'needs_permission'
}

/** Recompute the folder list Settings shows from the saved records. */
export function publishFolders() {
  const store = hostStore()
  const folders = [...store.records.values()].map(record => ({
    id: record.id,
    name: record.name,
    mode: record.mode,
    access: record.access,
    native: record.source === 'native',
    paused: Boolean(record.paused),
    state: folderState(record),
    fileCount: store.access.get(record.id)?.fileCount ?? null,
  }))
  publish({ folders })
}
