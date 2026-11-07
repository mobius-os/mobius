/* When a chat upload is big enough to suggest sharing its folder instead of storing copies. */
import { hostStore } from './hostState.js'

// Suggest sharing above 5 files or 10 MB.
export const SUGGEST_ABOVE_FILES = 5
export const SUGGEST_ABOVE_BYTES = 10 * 1024 * 1024

export function uploadTotals(files) {
  return { count: files.length, bytes: files.reduce((sum, file) => sum + (file.size || 0), 0) }
}

/** True when the upload is big and this browser can share a folder right now. */
export function shouldSuggestSharedFolder(files, state = hostStore().state) {
  const { count, bytes } = uploadTotals(files)
  if (count <= SUGGEST_ABOVE_FILES && bytes <= SUGGEST_ABOVE_BYTES) return false
  if (state.status !== 'ready') return false
  // Older Möbius phone app builds take over the folder picker and cannot share folders.
  if (globalThis.MobiusNative && !state.canPickNative) return false
  return Boolean(state.canRemember || state.canPickForVisit)
}

/**
 * The upload to hold until the owner chooses, or null when it can go now.
 * Files added while a choice is open join the held upload, so none are lost.
 */
export function holdUpload(held, files, state = hostStore().state) {
  if (!held && !shouldSuggestSharedFolder(files, state)) return null
  const all = [...(held?.files || []), ...files]
  return { files: all, ...uploadTotals(all) }
}
