import {
  clear as clearIdbStore,
  createStore,
  del as delIdbValue,
  get as getIdbValue,
  set as setIdbValue,
} from 'idb-keyval'
import { reclaimStoredStreamSnapshots } from './streamSnapshotCache.js'

function availableStorage(storage) {
  if (storage !== undefined) return storage
  try { return globalThis.sessionStorage ?? null } catch { return null }
}

const DRAFT_ENVELOPE = 'mobius-composer-draft'
const DRAFT_VERSION = 2
const DURABLE_DRAFT_DB = 'mobius-owner-drafts'
const DURABLE_DRAFT_STORE = 'drafts-v1'
const PENDING_HANDOFF_KEY = 'pending-draft'
const PENDING_HANDOFF_AUTOSEND_KEY = 'pending-draft-autosend'
const HANDOFF_AUTOSEND_PREFIX = 'draft-autosend:'
const durableDraftStore = createStore(DURABLE_DRAFT_DB, DURABLE_DRAFT_STORE)

// Same-document navigation must never depend on a fallible browser-storage
// round-trip. A present entry with raw:null is an intentional empty tombstone;
// absence means this module has not learned the chat's current value yet.
const liveDrafts = new Map()
const draftRevisions = new Map()

const NO_DURABLE_WRITE = Symbol('no-durable-draft-write')
const durableWrites = new Map()
let durableGeneration = 0
let lastDraftTimestamp = 0

function draftId(chatId) {
  return String(chatId)
}

function revisionOf(chatId) {
  return draftRevisions.get(draftId(chatId)) || 0
}

function rememberLiveDraft(chatId, raw, source, { advance = false } = {}) {
  const id = draftId(chatId)
  if (advance) draftRevisions.set(id, revisionOf(id) + 1)
  liveDrafts.set(id, { raw, source })
}

function queueDurableDraftWrite(chatId, raw) {
  const id = draftId(chatId)
  let state = durableWrites.get(id)
  if (state) {
    state.latest = raw
    return state.done
  }

  const generation = durableGeneration
  let resolveDone
  state = {
    latest: raw,
    done: new Promise(resolve => { resolveDone = resolve }),
  }
  durableWrites.set(id, state)

  const drain = async () => {
    while (state.latest !== NO_DURABLE_WRITE && generation === durableGeneration) {
      const next = state.latest
      state.latest = NO_DURABLE_WRITE
      try {
        if (next == null) await delIdbValue(id, durableDraftStore)
        else await setIdbValue(id, next, durableDraftStore)
      } catch {
        // The synchronous memory mirror remains authoritative for this page.
        // Session storage is attempted independently by persistComposerDraft.
      }
    }
  }

  const finishDrain = () => {
    // A microtask may enqueue a fresher value after drain's final loop check
    // but before its promise continuation runs. Re-open the drain rather than
    // resolving a write that never reached the database.
    if (state.latest !== NO_DURABLE_WRITE && generation === durableGeneration) {
      void drain().finally(finishDrain)
      return
    }
    if (durableWrites.get(id) === state) durableWrites.delete(id)
    resolveDone()
  }
  void drain().finally(finishDrain)
  return state.done
}

export function composerDraftRevision(chatId) {
  if (chatId == null) return 0
  return revisionOf(chatId)
}

export async function flushComposerDraftPersistence() {
  while (durableWrites.size > 0) {
    await Promise.all([...durableWrites.values()].map(state => state.done))
  }
}

/** Clear both the live mirror and the dedicated owner-draft database on logout. */
export async function clearDurableComposerDrafts() {
  durableGeneration += 1
  liveDrafts.clear()
  draftRevisions.clear()
  lastDraftTimestamp = 0
  for (const state of durableWrites.values()) state.latest = NO_DURABLE_WRITE
  await Promise.all([...durableWrites.values()].map(state => state.done))
  try { await clearIdbStore(durableDraftStore) } catch {}
}

// Test-only: emulate a document reload without deleting the durable database.
export function _clearComposerDraftMemoryForTests() {
  liveDrafts.clear()
  draftRevisions.clear()
  lastDraftTimestamp = 0
}

function attachmentMetadata(attachment) {
  return {
    name: attachment.name,
    size: Number.isFinite(attachment.size) ? attachment.size : 0,
    mime_type: typeof attachment.mime_type === 'string'
      ? attachment.mime_type
      : 'application/octet-stream',
  }
}

function isNamedAttachment(attachment) {
  return !!(
    attachment
    && typeof attachment.name === 'string'
    && attachment.name.length > 0
  )
}

// Live upload state is an explicit trust boundary: only a completed upload is
// safe to persist as a sendable draft. Unknown/future states fail closed.
function completedAttachments(attachments) {
  if (!Array.isArray(attachments)) return []
  return attachments
    .filter(attachment => isNamedAttachment(attachment) && attachment.status === 'done')
    .map(attachmentMetadata)
}

// Stored envelopes are intentionally status-less because persistence already
// crossed the completed-only boundary above. Reject status-bearing/malformed
// rows instead of accidentally blessing a future pending state on reload.
function storedAttachments(attachments) {
  if (!Array.isArray(attachments)) return []
  return attachments
    .filter(attachment => isNamedAttachment(attachment) && attachment.status === undefined)
    // The envelope stays status-less on disk, but the live composer boundary
    // is explicit: a successfully validated stored row is a completed upload.
    // Returning `done` prevents the mount persistence effect (and React strict
    // remount) from immediately filtering the restored attachment back out.
    .map(attachment => ({ ...attachmentMetadata(attachment), status: 'done' }))
}

function decodeDraft(raw) {
  if (!raw) return { input: '', attachments: [], updatedAt: null }
  try {
    const parsed = JSON.parse(raw)
    if (parsed?.type === DRAFT_ENVELOPE
        && (parsed.version === 1 || parsed.version === DRAFT_VERSION)) {
      if (Number.isFinite(parsed.updated_at)) {
        lastDraftTimestamp = Math.max(lastDraftTimestamp, parsed.updated_at)
      }
      return {
        input: typeof parsed.input === 'string' ? parsed.input : '',
        attachments: storedAttachments(parsed.attachments),
        updatedAt: Number.isFinite(parsed.updated_at) ? parsed.updated_at : null,
      }
    }
  } catch { /* legacy plain text */ }
  return {
    input: typeof raw === 'string' ? raw : '',
    attachments: [],
    updatedAt: null,
  }
}

function publicDraft(decoded) {
  return { input: decoded.input, attachments: decoded.attachments }
}

function encodeDraft(input, attachments) {
  const safeAttachments = completedAttachments(attachments)
  if (!input && safeAttachments.length === 0) return null
  // Date.now() can repeat across several keystrokes. A strictly monotonic
  // value lets async hydration distinguish a newer durable write from the
  // older session copy even when quota failed within the same millisecond.
  lastDraftTimestamp = Math.max(Date.now(), lastDraftTimestamp + 1)
  return JSON.stringify({
    type: DRAFT_ENVELOPE,
    version: DRAFT_VERSION,
    updated_at: lastDraftTimestamp,
    input: typeof input === 'string' ? input : '',
    attachments: safeAttachments.map(({ status: _status, ...attachment }) => attachment),
  })
}

/**
 * Reads either the current structured draft or a legacy plain-text draft.
 * Object URLs are deliberately not stored: they stop working when the chat
 * unmounts. Restored image cards point at the already-uploaded chat file.
 */
export function readComposerDraft(chatId, storage) {
  if (chatId == null) return { input: '', attachments: [] }
  const useLiveMirror = storage === undefined
  const id = draftId(chatId)
  if (useLiveMirror && liveDrafts.has(id)) {
    return publicDraft(decodeDraft(liveDrafts.get(id).raw))
  }

  const target = availableStorage(storage)
  if (!target) return { input: '', attachments: [] }
  try {
    const raw = target.getItem(`draft:${chatId}`)
    if (useLiveMirror && raw) rememberLiveDraft(id, raw, 'session')
    return publicDraft(decodeDraft(raw))
  } catch {
    return { input: '', attachments: [] }
  }
}

/**
 * Resolve the durable fallback after mount without overwriting a newer local
 * edit. Versioned session and IndexedDB values carry the same timestamp, so a
 * reload can recover a newer durable write even when quota left an older
 * session value behind. Legacy plain-text session drafts win over durable data
 * because app-to-chat handoffs still intentionally use that format.
 */
export async function readComposerDraftAsync(chatId) {
  if (chatId == null) return { input: '', attachments: [] }
  const id = draftId(chatId)
  const revisionAtStart = revisionOf(id)
  const current = liveDrafts.get(id)
  if (current?.source === 'live' || current?.source === 'durable') {
    return publicDraft(decodeDraft(current.raw))
  }

  let durableRaw = null
  try { durableRaw = await getIdbValue(id, durableDraftStore) } catch {}

  if (revisionOf(id) !== revisionAtStart) {
    return publicDraft(decodeDraft(liveDrafts.get(id)?.raw))
  }

  const latestCurrent = liveDrafts.get(id)
  const currentDecoded = decodeDraft(latestCurrent?.raw)
  const durableDecoded = decodeDraft(durableRaw)
  const currentIsLegacy = latestCurrent?.raw && currentDecoded.updatedAt == null
  const durableIsNewer = durableRaw && !currentIsLegacy && (
    !latestCurrent?.raw
    || (durableDecoded.updatedAt ?? -1) > (currentDecoded.updatedAt ?? -1)
  )

  if (durableIsNewer) {
    rememberLiveDraft(id, durableRaw, 'durable')
    return publicDraft(durableDecoded)
  }
  if (latestCurrent?.raw) {
    queueDurableDraftWrite(id, latestCurrent.raw)
    return publicDraft(currentDecoded)
  }
  if (durableRaw) {
    rememberLiveDraft(id, durableRaw, 'durable')
    return publicDraft(durableDecoded)
  }
  return { input: '', attachments: [] }
}

export function clearComposerDraft(chatId, storage) {
  if (chatId == null) return
  if (storage === undefined) {
    rememberLiveDraft(chatId, null, 'live', { advance: true })
    queueDurableDraftWrite(chatId, null)
  }
  const target = availableStorage(storage)
  if (!target) return
  try { target.removeItem(`draft:${chatId}`) } catch {}
}

/**
 * Persist a composer value immediately.
 *
 * This deliberately belongs on the input event path rather than only in a
 * React effect. A browser back gesture can commit navigation and unmount the
 * chat before passive effects run, especially while the mobile keyboard is
 * settling. Synchronous storage here makes the text durable before React gets
 * a chance to remove the composer.
 */
export function persistComposerDraft(chatId, input, attachments = [], storage) {
  const useDurableStore = storage === undefined
  if (chatId == null) return false
  const key = `draft:${chatId}`
  const value = encodeDraft(input, attachments)

  if (useDurableStore) {
    rememberLiveDraft(chatId, value, 'live', { advance: true })
    queueDurableDraftWrite(chatId, value)
  }

  const target = availableStorage(storage)
  // Dedicated memory + IndexedDB ownership does not depend on Web Storage
  // being exposed (private/opaque contexts can deny it altogether).
  if (!target) return useDurableStore

  try {
    if (value) target.setItem(key, value)
    else target.removeItem(key)
    return true
  } catch (error) {
    const quotaExceeded = error?.name === 'QuotaExceededError' || error?.code === 22
    if (!useDurableStore) return false
    if (!quotaExceeded) {
      // Security/privacy modes may expose a Storage object whose writes still
      // fail. The live mirror and independent durable write remain valid.
      return true
    }

    // Owner text outranks a regenerable stream-remount cache. Reclaim that
    // cache and retry once, but never delete this or another chat's draft.
    reclaimStoredStreamSnapshots(target)
    try {
      if (value) target.setItem(key, value)
      else target.removeItem(key)
    } catch {
      // The live mirror + dedicated IndexedDB write still preserve the draft.
    }
    return true
  }
}

/**
 * Stage text for a chat that another app surface is about to open.
 *
 * The per-chat draft is the durable owner. The unkeyed pending value lets the
 * destination claim the handoff immediately, while the exact-text autosend
 * markers are reserved for cross-document navigation where no mounted
 * ChatView can acknowledge a direct submit request.
 */
export function stageComposerHandoff(
  chatId,
  input,
  { allowEmpty = false, attachments = [], autoSend = false, storage } = {},
) {
  if (chatId == null || typeof input !== 'string') return false
  const hasAttachments = Array.isArray(attachments) && attachments.length > 0
  if (input.length === 0 && !hasAttachments && !allowEmpty) return false
  const persisted = persistComposerDraft(chatId, input, attachments, storage)
  const target = availableStorage(storage)
  if (!target) return persisted

  try {
    // A session has one navigation handoff at a time. Retire abandoned keyed
    // autosends before staging the replacement so visiting an older chat later
    // cannot unexpectedly submit a stale approval.
    const shouldAutoSend = !!input && autoSend
    const keepAutoSendKey = shouldAutoSend
      ? `${HANDOFF_AUTOSEND_PREFIX}${chatId}`
      : null
    const staleAutoSendKeys = []
    for (let i = 0; i < target.length; i += 1) {
      const key = target.key(i)
      if (key?.startsWith(HANDOFF_AUTOSEND_PREFIX) && key !== keepAutoSendKey) {
        staleAutoSendKeys.push(key)
      }
    }
    for (const key of staleAutoSendKeys) target.removeItem(key)

    if (input) target.setItem(PENDING_HANDOFF_KEY, input)
    else target.removeItem(PENDING_HANDOFF_KEY)
    if (shouldAutoSend) {
      target.setItem(PENDING_HANDOFF_AUTOSEND_KEY, input)
      target.setItem(`${HANDOFF_AUTOSEND_PREFIX}${chatId}`, input)
    } else {
      target.removeItem(PENDING_HANDOFF_AUTOSEND_KEY)
      target.removeItem(`${HANDOFF_AUTOSEND_PREFIX}${chatId}`)
    }
  } catch {
    // The keyed draft still survives through the live/durable owner whenever
    // the browser exposes it, so a failed convenience marker is non-fatal.
  }
  return persisted
}

export function readComposerHandoff(chatId, storage) {
  const target = availableStorage(storage)
  if (!target) return { draft: null, autoSendDraft: null }
  try {
    return {
      draft: target.getItem(PENDING_HANDOFF_KEY),
      // Prefer the chat-bound marker. The global key exists for compatibility
      // with the pre-chat-id handoff window but must never outrank identity.
      autoSendDraft: (chatId == null
        ? null
        : target.getItem(`${HANDOFF_AUTOSEND_PREFIX}${chatId}`))
        || target.getItem(PENDING_HANDOFF_AUTOSEND_KEY),
    }
  } catch {
    return { draft: null, autoSendDraft: null }
  }
}

/** Remove only markers that still belong to `input`; a newer handoff wins. */
export function consumeComposerHandoff(
  chatId,
  input,
  { autoSend = false, storage } = {},
) {
  const target = availableStorage(storage)
  if (!target || typeof input !== 'string') return
  try {
    if (target.getItem(PENDING_HANDOFF_KEY) === input) {
      target.removeItem(PENDING_HANDOFF_KEY)
    }
    if (target.getItem(PENDING_HANDOFF_AUTOSEND_KEY) === input) {
      target.removeItem(PENDING_HANDOFF_AUTOSEND_KEY)
    }
    const keyedAutoSend = `${HANDOFF_AUTOSEND_PREFIX}${chatId}`
    if (autoSend && target.getItem(keyedAutoSend) === input) {
      target.removeItem(keyedAutoSend)
    }
  } catch { /* unavailable browser storage */ }
}
