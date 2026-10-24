/* What the owner does in Settings → Shared folders: add, pick again, reconnect, pause, remove. */
import { api } from '../../api/client.js'
import { createHandleFolder } from './handleFolder.js'
import { createSnapshotFolder } from './snapshotFolder.js'
import {
  deleteFolderRecord, newFolderId, saveFolderRecord,
} from './folderStore.js'
import {
  announceFolderChange, permissionMode, reportFolders,
} from './folderHost.js'
import { hostStore, publishFolders } from './hostState.js'

/** Outcome of an owner action: { ok, name? } or { ok: false, message }. */
const done = { ok: true }
const failed = message => ({ ok: false, message })

async function commit(...records) {
  // Every record takes effect before any save is awaited, so a command that
  // arrives meanwhile never finds part of the change still undone.
  records.forEach(record => hostStore().records.set(record.id, record))
  for (const record of records) await saveFolderRecord(record)
  publishFolders()
  announceFolderChange()
  await reportFolders()
}

async function findSameFolder(handle) {
  for (const record of hostStore().records.values()) {
    if (record.handle && await record.handle.isSameEntry(handle).catch(() => false)) return record
  }
  return null
}

/**
 * Chrome and Edge: pick a folder the browser will remember.
 * Must run straight from the click: the picker needs the tap's activation.
 */
export async function addRememberedFolder(mode) {
  let handle
  try {
    handle = await globalThis.showDirectoryPicker({
      id: 'mobius-shared-folder', mode: mode === 'edit' ? 'readwrite' : 'read',
    })
  } catch (error) {
    if (error?.name === 'AbortError') return { ok: false, cancelled: true }
    return failed(pickerMessage(error))
  }
  const existing = await findSameFolder(handle)
  const record = existing
    ? { ...existing, handle, mode, paused: false }
    : { id: newFolderId(), name: handle.name, mode, access: 'remembered', handle, paused: false, createdAt: Date.now() }
  const store = hostStore()
  store.permissions.set(record.id, 'granted')
  store.access.set(record.id, createHandleFolder(handle))
  await commit(record)
  return { ...done, name: record.name, updated: Boolean(existing) }
}

function pickerMessage(error) {
  if (error?.name === 'SecurityError' || error?.name === 'NotAllowedError') {
    return 'The browser blocked that folder. Pick a folder inside it, such as one inside Downloads.'
  }
  return 'The browser could not open that folder. Try another one.'
}

/** Safari, Firefox and Brave: a read-only copy of the picked folder's file list, for this visit. */
export async function addVisitFolder(fileList, replaceId = null) {
  const files = [...(fileList || [])]
  if (!files.length) return failed('That folder has no files the browser could share.')
  const folder = createSnapshotFolder(files)
  const store = hostStore()
  const existing = replaceId ? store.records.get(replaceId) : null
  const record = existing
    ? { ...existing, name: folder.rootName || existing.name, paused: false }
    : { id: newFolderId(), name: folder.rootName || 'Folder', mode: 'read', access: 'visit', paused: false, createdAt: Date.now() }
  store.access.set(record.id, folder)
  await commit(record)
  return { ...done, name: record.name }
}

/** Chrome or Edge forgot the permission: ask again, from the owner's tap. */
export async function reconnectFolder(id) {
  const store = hostStore()
  const record = store.records.get(id)
  if (!record?.handle) return failed('This folder can no longer be reconnected. Remove it and add it again.')
  let permission
  try {
    permission = await record.handle.requestPermission({ mode: permissionMode(record) })
  } catch {
    permission = 'denied'
  }
  store.permissions.set(id, permission)
  if (permission !== 'granted') return failed('The browser did not allow access. Try again, or add the folder again.')
  store.access.set(id, createHandleFolder(record.handle))
  await commit({ ...record, paused: false })
  return done
}

export async function setFolderPaused(id, paused) {
  const record = hostStore().records.get(id)
  if (!record) return failed('This folder is no longer shared.')
  await commit({ ...record, paused })
  return done
}

/** Pause every folder this browser shares, or only the ones named. */
export async function pauseAllFolders(ids = null) {
  const paused = [...hostStore().records.values()]
    .filter(record => !record.paused && (!ids || ids.includes(record.id)))
    .map(record => ({ ...record, paused: true }))
  if (paused.length) await commit(...paused)
  return done
}

/**
 * The Stop button, on any device: the server refuses every shared folder at
 * once and tells the browsers holding them. This browser pauses its own
 * right away too, so Stop works here even if the server can't be reached.
 */
export async function stopEverywhere() {
  await Promise.allSettled([pauseAllFolders(), api.sharedFolders.pause()])
  return done
}

/** Another device tapped Stop: keep the pause here, where the folder lives. */
export async function applyRemotePause(event) {
  const ids = Array.isArray(event?.folderIds) ? event.folderIds : []
  if (ids.length) await pauseAllFolders(ids)
}

/** Stop sharing a folder. The folder and its files stay on the device. */
export async function removeFolder(id) {
  const store = hostStore()
  store.records.delete(id)
  store.access.delete(id)
  store.permissions.delete(id)
  await deleteFolderRecord(id)
  publishFolders()
  announceFolderChange()
  await reportFolders()
  return done
}
