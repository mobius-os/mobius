import { clear, createStore, del, entries, get, set, update } from 'idb-keyval'
import { sendDraftIdentity } from './sendAttemptIdentity.js'

// Durable owner intent for the two ordinary chat writes: sending a message and
// answering a question. useStreamConnection records the complete POST body here
// before transport begins, keyed by the same client cid the backend deduplicates.
// A lost acknowledgement can therefore be replayed without starting a second
// turn. Steers are deliberately excluded because they mutate one live turn and
// are not safe to apply after that turn has moved on.

const OUTBOX_DB = 'mobius-chat-outbox'
const OUTBOX_STORE = 'intents-v1'
const store = createStore(OUTBOX_DB, OUTBOX_STORE)

// One authorization rejection gets one attempt per loaded document. Keeping the
// record preserves an expired owner's intent, while this memory-only latch keeps
// a permanent 403 from being POSTed on every focus/visibility event. A new login
// reloads the document and gets one fresh attempt with the new credential.
const authBlockedAttempted = new Set()
// A failed IndexedDB transition cannot be allowed to race a later drain in the
// same document. The durable retired marker below is the cross-reload owner;
// this fence only closes the smaller in-process gap while that transition is
// unavailable or still committing.
const retirementFences = new Set()
let clearGeneration = 0
let retirementSequence = 0

/**
 * A non-secret partition key for the principal represented by a JWT.
 *
 * This is only a browser-storage ownership boundary; the server still verifies
 * the signed token on every replay. Owner sessions remain stable across token
 * renewal through sub+epoch. Embedded chats are additionally bound to their
 * capability scope, app, and chat so the owner shell cannot inherit their
 * queued text. Invalid/opaque tokens opt out of persistence rather than putting
 * owner-authored content into an unscoped database.
 */
export function outboxPrincipalKey(token) {
  try {
    const payload = String(token || '').split('.')[1]
    if (!payload || typeof atob !== 'function') return null
    const padded = payload.replace(/-/g, '+').replace(/_/g, '/')
      .padEnd(Math.ceil(payload.length / 4) * 4, '=')
    const binary = atob(padded)
    const bytes = Uint8Array.from(binary, char => char.charCodeAt(0))
    const claims = JSON.parse(new TextDecoder().decode(bytes))
    if (typeof claims?.sub !== 'string' || !claims.sub) return null
    const scope = claims.scope || 'owner'
    if (scope !== 'owner' && scope !== 'chat_embed') return null
    return JSON.stringify([
      claims.sub,
      claims.epoch ?? 0,
      scope,
      scope === 'chat_embed' ? String(claims.app_id ?? '') : '',
      scope === 'chat_embed' ? String(claims.chat_id ?? '') : '',
    ])
  } catch {
    return null
  }
}

function sameOwnerPartition(first, second) {
  try {
    const [firstSubject, firstEpoch] = JSON.parse(first)
    const [secondSubject, secondEpoch] = JSON.parse(second)
    return firstSubject === secondSubject && firstEpoch === secondEpoch
  } catch {
    return false
  }
}

export function storedIntentOwnership(recordPrincipalKey, currentPrincipalKey) {
  if (!recordPrincipalKey) return 'discard'
  if (recordPrincipalKey === currentPrincipalKey) return 'owned'
  return sameOwnerPartition(recordPrincipalKey, currentPrincipalKey)
    ? 'preserve'
    : 'discard'
}

// A replay's terminal disposition, decided from an HTTP result:
//   delivered — accepted, duplicate, or an answer whose question is gone;
//   retry     — transport/rate/server trouble; preserve order and retry later;
//   auth      — keep, but attempt only once per loaded document;
//   failed    — an authoritative client rejection; the interactive caller
//               restores the draft, so silently replaying it later is wrong.
export function classifyReplayOutcome({ ok, status }) {
  if (ok || status === 410) return 'delivered'
  if (status === 401 || status === 403) return 'auth'
  if (status === 408 || status === 425 || status === 429 || status >= 500) {
    return 'retry'
  }
  return 'failed'
}

const settlementSubscribers = new Set()

export function subscribeOutboxSettlement(callback) {
  settlementSubscribers.add(callback)
  return () => { settlementSubscribers.delete(callback) }
}

function announceSettlement(record, outcome) {
  const settlement = {
    chatId: String(record.chatId),
    cid: String(record.cid),
    outcome,
  }
  for (const callback of settlementSubscribers) {
    try { callback(settlement) } catch { /* a listener cannot stall the drain */ }
  }
}

async function persistRecord(record) {
  try {
    await set(record.cid, record, store)
    return true
  } catch {
    return false
  }
}

export async function enqueueIntent({ chatId, cid, type, body, principalKey }) {
  if (!chatId || !cid || !body) return false
  if (!principalKey) return false
  const key = String(cid)
  try {
    await update(key, existing => ({
      chatId: String(chatId),
      cid: key,
      type: type || 'message',
      body,
      principalKey,
      createdAt: (
        (!existing?.principalKey || existing.principalKey === principalKey)
          ? Number(existing?.createdAt || Date.now())
          : Date.now()
      ),
      authBlocked: false,
    }), store)
    retirementFences.delete(key)
    return true
  } catch {
    return false
  }
}

function retirementId() {
  retirementSequence += 1
  return `${Date.now()}:${retirementSequence}`
}

function transitionIntentToRetired(key, { chatId, outcome }) {
  return store('readwrite', objectStore => new Promise((resolve, reject) => {
    const transaction = objectStore.transaction
    let result = { state: 'absent' }
    transaction.oncomplete = () => resolve(result)
    transaction.onerror = () => reject(transaction.error || new Error('outbox retirement failed'))
    transaction.onabort = () => reject(transaction.error || new Error('outbox retirement aborted'))

    const request = objectStore.get(key)
    request.onsuccess = () => {
      const record = request.result
      if (!record || (chatId && String(record.chatId) !== String(chatId))) return
      if (record.replayState === 'retired' && record.retirement?.id) {
        result = {
          state: 'already-retired',
          record,
          retirement: record.retirement,
        }
        return
      }
      const retirement = {
        id: retirementId(),
        outcome: outcome || 'discarded',
      }
      result = { state: 'retired', record, retirement }
      objectStore.put({
        ...record,
        replayState: 'retired',
        retirement,
      }, key)
    }
  }))
}

// Delete is only compaction. It re-checks the retirement token in the same
// transaction so a manual resend that re-activates this cid cannot be deleted
// by an older retirement cleanup.
function cleanupRetiredIntent(key, expectedRetirementId) {
  return store('readwrite', objectStore => new Promise((resolve, reject) => {
    const transaction = objectStore.transaction
    let deleted = false
    transaction.oncomplete = () => resolve(deleted)
    transaction.onerror = () => reject(transaction.error || new Error('outbox cleanup failed'))
    transaction.onabort = () => reject(transaction.error || new Error('outbox cleanup aborted'))

    const request = objectStore.get(key)
    request.onsuccess = () => {
      const record = request.result
      if (
        record?.replayState !== 'retired'
        || record.retirement?.id !== expectedRetirementId
      ) return
      deleted = true
      objectStore.delete(key)
    }
  }))
}

/**
 * Authoritatively retire replay eligibility before best-effort compaction.
 *
 * A successful return means either no matching row exists or the row is
 * durably marked non-replayable. `cleanup` is injectable so callers can prove
 * that a failed physical delete cannot undo that state transition.
 */
export async function retireIntent(
  cid,
  { chatId = null, outcome = null } = {},
  { cleanup = cleanupRetiredIntent } = {},
) {
  if (!cid) return false
  const key = String(cid)
  retirementFences.add(key)
  let transition
  try {
    transition = await transitionIntentToRetired(key, { chatId, outcome })
  } catch {
    return false
  }
  if (transition.state === 'absent') {
    retirementFences.delete(key)
    authBlockedAttempted.delete(key)
    return true
  }

  authBlockedAttempted.delete(key)
  if (
    transition.state === 'retired'
    && (outcome === 'delivered' || outcome === 'failed')
  ) {
    // The record owns its chat identity; never let a caller-supplied label move
    // a private cid settlement onto another mounted chat.
    announceSettlement(transition.record, outcome)
  }
  // Compaction is deliberately outside the awaited state boundary. A blocked
  // physical delete must not hold an accepted response or cancellation after
  // replay eligibility is already durably retired.
  void Promise.resolve()
    .then(() => cleanup(key, transition.retirement.id))
    .then(
      () => { retirementFences.delete(key) },
      () => {
        // The durable marker is authoritative. Keeping the fence also protects
        // a drain that captured the old row before the marker committed.
      },
    )
  return true
}

function replayBodyMatchesAttempt(record, attempt, chatId, key) {
  const body = record?.body
  if (
    record?.type !== 'message'
    || record.replayState === 'retired'
    || !body
    || typeof body !== 'object'
    || String(body.cid) !== key
    || typeof body.content !== 'string'
    || body.hidden === true
    || body.answers != null
    || body.question_id != null
    || body.continuation != null
    || body.force_steer === true
    || body.direct_steer === true
    || !attempt
    || String(attempt.cid) !== key
    || typeof attempt.text !== 'string'
    || typeof attempt.transportContent !== 'string'
    || typeof attempt.draftIdentity !== 'string'
    || body.content !== attempt.transportContent
  ) return false

  const expectedIdentity = sendDraftIdentity(
    chatId,
    attempt.text,
    attempt.attachments || [],
  )
  return attempt.draftIdentity === expectedIdentity
    && sendDraftIdentity(chatId, attempt.text, body.attachments || []) === expectedIdentity
}

/**
 * Inspect one exact durable intent without scanning, pruning, or adopting data.
 * An unreadable store or invalid principal is unknown; a row is retained only
 * when its key, owner, chat, cid, and replay body all name the same intent.
 */
export async function inspectOutboxIntent({ chatId, cid, principalKey, attempt }) {
  if (!principalKey) return 'unknown'
  if (!chatId || !cid) return 'absent'
  const key = String(cid)
  try {
    const record = await get(key, store)
    if (!record) return 'absent'
    return (
      record.principalKey === principalKey
      && String(record.chatId) === String(chatId)
      && String(record.cid) === key
      && replayBodyMatchesAttempt(record, attempt, chatId, key)
    ) ? 'retained' : 'absent'
  } catch {
    return 'unknown'
  }
}

export async function listIntents(principalKey) {
  if (!principalKey) return []
  try {
    const rows = await entries(store)
    const owned = []
    const cleanup = []
    for (const [key, value] of rows) {
      if (value?.replayState === 'retired') {
        if (value.retirement?.id) {
          cleanup.push(
            cleanupRetiredIntent(String(key), value.retirement.id)
              .then(() => { retirementFences.delete(String(key)) }),
          )
        }
        continue
      }
      if (retirementFences.has(String(key))) continue
      if (!value?.cid || !value?.chatId || !value?.body) {
        cleanup.push(del(key, store))
        continue
      }
      const ownership = storedIntentOwnership(value.principalKey, principalKey)
      if (ownership === 'discard') {
        // Ownership of an unscoped row cannot be proven, and a different
        // owner/epoch is known stale. Delete both rather than guessing across
        // this security boundary.
        cleanup.push(del(key, store))
        continue
      }
      if (ownership === 'preserve') {
        // Capabilities for the same owner share this origin but not replay
        // authority. Keep the row for its own mounted chat.
        continue
      }
      owned.push(value)
    }
    await Promise.allSettled(cleanup)
    return owned.sort((a, b) => (
      Number(a.createdAt || 0) - Number(b.createdAt || 0)
      || String(a.cid).localeCompare(String(b.cid))
    ))
  } catch {
    return []
  }
}

export const REPLAY_TIMEOUT_MS = 15_000

export function outboxRequestPath(chatId) {
  return `/chats/${encodeURIComponent(String(chatId))}/messages`
}

export async function deliverIntent(record, request) {
  if (typeof request !== 'function') throw new TypeError('outbox request is required')
  let response
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), REPLAY_TIMEOUT_MS)
  try {
    response = await request(record, { signal: controller.signal })
  } catch (error) {
    if (error?.message === 'AUTH_EXPIRED' || error?.message === 'EMBED_AUTH_EXPIRED') {
      return 'auth'
    }
    return 'retry'
  } finally {
    clearTimeout(timer)
  }
  return classifyReplayOutcome({ ok: response.ok, status: response.status })
}

async function drainInner({ deliver, principalKey, generation }) {
  const records = await listIntents(principalKey)
  if (generation !== clearGeneration) return
  for (const record of records) {
    if (retirementFences.has(String(record.cid))) continue
    if (record.authBlocked && authBlockedAttempted.has(record.cid)) break
    if (record.authBlocked) authBlockedAttempted.add(record.cid)

    const outcome = await deliver(record)
    // Logout/owner cleanup may race a bounded request already on the wire. Its
    // result must never repopulate or announce data after the owner store was
    // cleared.
    if (generation !== clearGeneration) return
    if (outcome === 'retry') {
      authBlockedAttempted.delete(record.cid)
      if (record.authBlocked) {
        await persistRecord({ ...record, authBlocked: false })
      }
      // Preserve accepted order and avoid N timeout/server attempts while one
      // earlier intent has already proved transport unavailable.
      break
    }
    if (outcome === 'auth') {
      authBlockedAttempted.add(record.cid)
      if (!record.authBlocked) {
        await persistRecord({ ...record, authBlocked: true })
      }
      break
    }

    const retired = await retireIntent(record.cid, {
      chatId: record.chatId,
      outcome,
    })
    if (!retired) break
  }
}

// Same-document events coalesce through the promise; Web Locks serialize the
// database across tabs. The server's cid gate remains the correctness backstop.
let inFlight = null
export function drainOutbox({ deliver, principalKey }) {
  if (typeof deliver !== 'function' || !principalKey) return Promise.resolve()
  if (inFlight) return inFlight
  const generation = clearGeneration
  const run = () => (
    (typeof navigator !== 'undefined' && navigator.locks?.request)
      ? navigator.locks.request(
          'mobius-chat-outbox',
          () => drainInner({ deliver, principalKey, generation }),
        )
      : drainInner({ deliver, principalKey, generation })
  )
  const tracked = Promise.resolve().then(run).finally(() => {
    if (inFlight === tracked) inFlight = null
  })
  inFlight = tracked
  return tracked
}

export async function clearChatOutbox() {
  clearGeneration += 1
  inFlight = null
  authBlockedAttempted.clear()
  retirementFences.clear()
  try {
    await clear(store)
    return true
  } catch {
    return false
  }
}

export async function clearOutboxForTests() {
  await clearChatOutbox()
}

export function resetOutboxReplaySessionForTests() {
  authBlockedAttempted.clear()
  retirementFences.clear()
  inFlight = null
}
