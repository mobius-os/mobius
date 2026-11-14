// A visible app may ask the shell to confirm one sensitive action with the
// owner. The server describes the action in words, the shell draws its own
// sheet, and only the owner's tap on that sheet mints a short-lived, single-use
// grant with the owner's bearer. The app id comes from AppCanvas and the words
// from the server, never from the message payload; the frame never receives a
// grant, it just repeats its normal request afterwards.
import { api } from '../api/client.js'

export const OWNER_GRANT_ARM_MS = 500
const MAX_BODY_CHARS = 64 * 1024
const MAX_SCOPE_KEYS = 16
const MAX_SCOPE_VALUE_CHARS = 256
const ACTION_ID = /^[a-z][a-z_]{0,39}$/
const SCOPE_KEY = /^[A-Za-z_][A-Za-z0-9_]{0,39}$/
const CHANGED_NOTICE = 'This changed after you looked.'

function isPlainObject(value) {
  return Object.prototype.toString.call(value) === '[object Object]'
    && [Object.prototype, null].includes(Object.getPrototypeOf(value))
}

function parseScope(scope) {
  if (!isPlainObject(scope)) return null
  const entries = Object.entries(scope)
  const valid = entries.length <= MAX_SCOPE_KEYS && entries.every(([key, value]) => (
    SCOPE_KEY.test(key) && typeof value === 'string' && value.length <= MAX_SCOPE_VALUE_CHARS))
  return valid ? Object.fromEntries(entries) : null
}

// The JSON round trip keeps only what the server will see, so what the owner
// confirms is what is sent.
function parseBody(body) {
  if (!isPlainObject(body)) return null
  try {
    const json = JSON.stringify(body)
    return json.length <= MAX_BODY_CHARS ? JSON.parse(json) : null
  } catch { return null }
}

export function parseAppOwnerGrant(message) {
  if (message?.type !== 'moebius:owner-grant') return null
  const { requestId, action } = message
  if (typeof requestId !== 'string' || !requestId || requestId.length > 160) return null
  if (typeof action !== 'string' || !ACTION_ID.test(action)) return null
  const scope = parseScope(message.scope)
  const body = parseBody(message.body)
  return scope && body ? { requestId, action, scope, body } : null
}

function answerFrame(source, type, requestId, fields = {}) {
  if (typeof requestId !== 'string' || requestId.length > 160) return
  try {
    source.postMessage({ type, requestId, ...fields }, '*')
  } catch { /* the frame may have been retired */ }
}

function replyOwnerGrant(source, requestId, result) {
  answerFrame(source, 'moebius:owner-grant-result', requestId, result)
}

// The prompt gate, the same as the file export's: a buffered or hidden frame
// never occupies the one confirmation slot. The pending record moves through
// phases: checking (the server is describing it), ready (the sheet shows the
// server's words), confirming (minting) and error.
export function beginAppOwnerGrant({ message, source, sourceVersion, liveVersion, visible, pending, appId }) {
  if (!visible || sourceVersion == null || sourceVersion !== liveVersion) {
    replyOwnerGrant(source, message.requestId, { cancelled: true })
    return null
  }
  const parsed = parseAppOwnerGrant(message)
  if (!parsed) {
    replyOwnerGrant(source, message.requestId, { error: 'Invalid confirmation request.' })
    return null
  }
  if (pending) {
    replyOwnerGrant(source, parsed.requestId, { error: 'Finish the current confirmation first.' })
    return null
  }
  // The ack tells the frame its request was accepted, so it stops waiting for
  // a shell that never answers; the owner may then take as long as they like.
  answerFrame(source, 'moebius:owner-grant-ack', parsed.requestId)
  return {
    ...parsed, source, appId, frameVersion: sourceVersion,
    phase: 'checking', controller: new AbortController(),
  }
}

export function finishAppOwnerGrant({ request, pendingRef, setPending, result }) {
  if (pendingRef.current !== request) return false
  request.controller.abort()
  pendingRef.current = null
  setPending(null)
  replyOwnerGrant(request.source, request.requestId, result)
  return true
}

// The owner closed the sheet (or it was torn down): a sheet that had already
// failed reports its failure, any other close is a cancel.
export function closeAppOwnerGrant({ pendingRef, setPending }) {
  const request = pendingRef.current
  if (!request) return false
  const result = request.phase === 'error'
    ? { error: request.error, code: request.code }
    : { cancelled: true }
  return finishAppOwnerGrant({ request, pendingRef, setPending, result })
}

// Swap in a changed copy of the pending record; null once it was finished.
function advance({ request, pendingRef, setPending }, patch) {
  if (pendingRef.current !== request) return null
  const next = { ...request, ...patch }
  pendingRef.current = next
  setPending(next)
  return next
}

function fail(slot, error) {
  if (error?.name === 'AbortError') return
  advance(slot, {
    phase: 'error', code: error?.code,
    error: error?.status ? error.message : 'Could not reach Möbius. Nothing was confirmed.',
  })
}

function grantPayload({ appId, action, scope, body }) {
  return { app_id: Number(appId), action, scope, body }
}

export async function previewAppOwnerGrant({ request, pendingRef, setPending, notice = null }) {
  const slot = { request, pendingRef, setPending }
  try {
    const preview = await api.ownerGrants.preview(grantPayload(request), { signal: request.controller.signal })
    advance(slot, { phase: 'ready', preview, notice })
  } catch (error) {
    fail(slot, error)
  }
}

export async function mintAppOwnerGrant({ request, pendingRef, setPending }) {
  const confirming = advance({ request, pendingRef, setPending }, { phase: 'confirming' })
  if (!confirming) return
  const slot = { request: confirming, pendingRef, setPending }
  try {
    await api.ownerGrants.mint(
      { ...grantPayload(confirming), digest: confirming.preview.digest },
      { signal: confirming.controller.signal },
    )
    finishAppOwnerGrant({ ...slot, result: { ok: true } })
  } catch (error) {
    if (error?.code !== 'owner_grant_changed') return fail(slot, error)
    // The facts moved after the owner looked: show the new words, never mint
    // against the old ones.
    const checking = advance(slot, { phase: 'checking' })
    if (checking) await previewAppOwnerGrant({ request: checking, pendingRef, setPending, notice: CHANGED_NOTICE })
  }
}
