/* This tab's side of shared folders: report them to Möbius and answer the agent's commands. */
import { api, isSharedBrowserAuth } from '../../api/client.js'
import { FolderError } from './folderPaths.js'
import { createHandleFolder } from './handleFolder.js'
import { deviceId, deviceLabel, loadFolderRecords } from './folderStore.js'
import {
  folderState, hostStore, publish, publishFolders,
} from './hostState.js'

export const SHARED_FOLDER_COMMAND_EVENT = 'shared_folder_command'
export const SHARED_FOLDER_ACTIVITY_EVENT = 'shared_folder_activity'
export const SHARED_FOLDER_PAUSE_EVENT = 'shared_folder_pause'
const CHANNEL_NAME = 'mobius-shared-folders'
const EDIT_OPERATIONS = new Set(['write', 'move', 'mkdir', 'bin'])
const ACTIVITY_VISIBLE_MS = 6000
// The server answers or gives up on every command within 30 s, and says so.
// A pill whose "done" was lost with a dropped connection clears after this.
const ACTIVITY_STALE_MS = 45_000

let channel = null
let activityTimer = null

/** Start once per page. Safe to call again; later calls do nothing. */
export async function startSharedFolderHost() {
  const store = hostStore()
  if (store.started) return
  store.started = true
  if (isSharedBrowserAuth() || (!store.state.canRemember && !store.state.canPickForVisit)) {
    publish({ status: 'unavailable' })
    return
  }
  await reloadRecords()
  await checkServer()
  listenAcrossTabs()
  // The stream may have connected while the saved folders were loading.
  await reportFolders()
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') void refreshPermissions()
    })
  }
}

/** Bind this tab's folders to its live system stream, as visible-app presence does. */
export async function setSharedFolderSubscription(subscriptionId) {
  const store = hostStore()
  store.subscriptionId = subscriptionId || null
  // Activity arrives on the stream; a new stream starts with none shown.
  clearTimeout(activityTimer)
  if (store.state.activity) publish({ activity: null })
  if (!store.subscriptionId) return
  // A restart that added the feature arrives as a new stream: look again.
  if (store.state.status === 'restart') await checkServer()
  await reportFolders()
}

export async function reloadRecords() {
  const store = hostStore()
  let records = []
  try {
    records = await loadFolderRecords()
  } catch {
    // Private browsing can refuse IndexedDB; keep whatever this tab holds.
    records = [...store.records.values()]
  }
  store.records = new Map(records.map(record => [record.id, record]))
  for (const id of store.access.keys()) {
    if (!store.records.has(id)) store.access.delete(id)
  }
  await refreshPermissions({ report: false })
}

async function checkServer() {
  try {
    const response = await api.sharedFolders.list()
    publish({ status: response.status === 404 ? 'restart' : 'ready' })
  } catch {
    publish({ status: 'ready' })
  }
}

/** Ask the browser which remembered folders still have permission. */
export async function refreshPermissions({ report = true } = {}) {
  const store = hostStore()
  for (const record of store.records.values()) {
    if (record.access !== 'remembered' || !record.handle) continue
    let permission = 'prompt'
    try {
      permission = await record.handle.queryPermission({ mode: permissionMode(record) })
    } catch {
      permission = 'denied'
    }
    store.permissions.set(record.id, permission)
    if (permission === 'granted' && !store.access.has(record.id)) {
      store.access.set(record.id, createHandleFolder(record.handle))
    }
  }
  publishFolders()
  if (report) await reportFolders()
}

export function permissionMode(record) {
  return record.mode === 'edit' ? 'readwrite' : 'read'
}

export async function reportFolders() {
  const store = hostStore()
  if (!store.subscriptionId || store.state.status !== 'ready') return
  store.sequence += 1
  const folders = [...store.records.values()].map(record => ({
    id: record.id,
    name: record.name,
    mode: record.mode,
    access: record.access,
    state: folderState(record),
  }))
  try {
    // A stream that is already gone answers 404; its successor reports afresh.
    await api.sharedFolders.report(store.subscriptionId, {
      sequence: store.sequence, deviceId: deviceId(), deviceLabel: deviceLabel(), folders,
    })
  } catch {
    // Offline: the next stream connection reports again.
  }
}

/** Tell other Möbius tabs in this browser that the saved folders changed. */
export function announceFolderChange() {
  channel?.postMessage({ type: 'changed' })
}

function listenAcrossTabs() {
  if (typeof BroadcastChannel !== 'function') return
  channel = new BroadcastChannel(CHANNEL_NAME)
  channel.onmessage = async () => {
    await reloadRecords()
    await reportFolders()
  }
}

/** Run one agent command against this tab's folder and send back the answer. */
export async function handleSharedFolderCommand(event) {
  const store = hostStore()
  const subscriptionId = store.subscriptionId
  if (!event?.commandId || !subscriptionId) return
  let answer
  try {
    const result = await runCommand(event)
    // Stop or Remove while a read ran: its contents never leave the device.
    // A finished change has happened, so it is reported as it is.
    if (!EDIT_OPERATIONS.has(event.op)) allowedFolder(event.folderId, false)
    answer = { ok: true, result }
  } catch (error) {
    const known = error instanceof FolderError
    answer = {
      ok: false,
      code: known ? error.code : 'device_error',
      error: String(error?.message || error).slice(0, 1000),
    }
    if (known && error.code === 'needs_permission') void refreshPermissions()
  }
  await api.sharedFolders.respond({ subscriptionId, commandId: event.commandId, ...answer }).catch(() => {})
}

async function runCommand(event) {
  const command = { ...event }
  delete command.type
  delete command.commandId
  delete command.folderId
  delete command.startWithinMs
  if (!EDIT_OPERATIONS.has(event.op)) return allowedFolder(event.folderId, false).run(command)
  // A change checks its destination, then writes it. One change at a time per
  // folder, across this browser's tabs, so two can never claim the same name.
  // The owner can pause, remove or narrow the folder while a change waits, so
  // its rights are checked once it holds the lock. The callback is async so a
  // refusal rejects its promise: Node 24 keeps a lock whose callback throws.
  const startBy = performance.now() + event.startWithinMs
  return navigator.locks.request(`mobius-shared-folder:${event.folderId}`, async () => {
    // The server stops waiting soon after; starting now could repeat the
    // agent's retry, so a change that waited too long is not made at all.
    if (performance.now() > startBy) {
      throw new FolderError('busy', 'An earlier change in this folder took too long, so this one was not made. Try again.')
    }
    return allowedFolder(event.folderId, true).run(command)
  })
}

/** The folder's access as it stands now, or the reason it is refused. */
function allowedFolder(folderId, editing) {
  const store = hostStore()
  const record = store.records.get(folderId)
  if (!record) throw new FolderError('not_found', 'This folder is no longer shared from this browser.')
  const state = folderState(record)
  if (state !== 'ready') throw new FolderError(state, notReadyMessage(state))
  if (editing && (record.mode !== 'edit' || record.access !== 'remembered')) {
    throw new FolderError('read_only', 'This folder is shared as Read only.')
  }
  return store.access.get(record.id)
}

function notReadyMessage(state) {
  const messages = {
    paused: 'The owner paused this folder. Ask them to resume it in Settings → Shared folders.',
    needs_pick: 'This browser forgets folders between visits. Ask the owner to tap Pick again in Settings → Shared folders.',
    needs_permission: 'The browser needs the owner to tap Reconnect for this folder in Settings → Shared folders.',
  }
  return messages[state] || 'This folder is not ready.'
}

/**
 * What agents are working in, on any of the owner's devices, as the server
 * reports it. The pill and its Stop stay while any command runs, then a few
 * seconds more.
 */
export function showSharedFolderActivity(event) {
  clearTimeout(activityTimer)
  if (!(event?.commands > 0)) {
    activityTimer = setTimeout(() => publish({ activity: null }), ACTIVITY_VISIBLE_MS)
    return
  }
  publish({
    activity: {
      folderId: event.folderId,
      folderName: event.folderName,
      deviceLabel: event.deviceId === deviceId() ? null : event.deviceLabel,
    },
  })
  activityTimer = setTimeout(() => publish({ activity: null }), ACTIVITY_STALE_MS)
}
