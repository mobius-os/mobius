// Protocol for the in-app agent-chat embed (capability A, design §1).
//
// A mini-app calls `window.mobius.chat({chatId?, ...})` (mobius-runtime.js)
// which mounts a nested iframe at `/shell/embed/chat`. The outer app sandbox
// makes both app and nested documents opaque (`event.origin === "null"`). The
// route is inert until it exchanges a one-use server capability delivered in
// memory by INIT; neither chat ids nor bearer material enter its URL.
//
// This module is the single source of truth for the postMessage shapes
// exchanged between the app frame (parent) and the embed frame (child).
// It is bundled into the shell (ChatEmbed.jsx imports it) and mirrored —
// NOT imported — by mobius-runtime.js, which is served verbatim from
// /public and so can't import a Vite-hashed /src module. The mirror is
// deliberately tiny (the NS prefix + the source/origin guard); keep the
// two in sync the way app-frame.html ↔ AppCanvas.jsx already do.
//
// Exact source-window and instance checks prevent accidental/spoofed routing in
// the browser. They are defense in depth only: null origin, window identity,
// correlation ids and a successful handshake are not server authorization.

// All embed messages carry this prefix. Greppable, and distinct from the
// app-frame protocol's `moebius:frame-*` / `moebius:nav-*` / `moebius:app-*`
// namespaces so a stray app-frame message can never be mistaken for an
// embed message even on the shared origin.
export const NS = 'moebius:chat-embed:'

// Child (embed frame) → parent (app frame).
export const READY = NS + 'ready' // embed mounted; carries the resolved chatId
export const MESSAGE_SENT = NS + 'message-sent' // a user turn was submitted
export const TURN_DONE = NS + 'turn-done' // the agent turn finished streaming
export const ERROR = NS + 'error' // the embed hit a load/stream error
export const HEIGHT = NS + 'height' // optional content height (see note below)
export const AUTH_EXPIRING = NS + 'auth-expiring' // parent should mint a new grant
export const BOOTSTRAP_READY = NS + 'bootstrap-ready' // child listener is installed

// Parent (app frame) → child (embed frame).
export const INIT = NS + 'init' // hand the embed its config + correlation id

// Context protocol (send-time app state injection).
//
// Before the child submits a user message it posts CONTEXT_REQUEST to the
// parent. The parent calls opts.getContext() if provided and posts
// CONTEXT_RESPONSE back. The child waits for a short bounded window then sends
// regardless — the protocol is best-effort, never an unbounded send blocker.
export const CONTEXT_REQUEST = NS + 'context-request'  // child → parent: {nonce}
export const CONTEXT_RESPONSE = NS + 'context-response' // parent → child: {nonce, context}
// Two opaque-frame postMessage hops can exceed one animation frame on a busy
// device/CI worker. 250ms remains imperceptible on the exceptional no-response
// path while avoiding silent context loss under ordinary main-thread pressure.
export const CONTEXT_RESPONSE_TIMEOUT_MS = 250

// Why no height relay by default: the design (§1.2) calls for a
// FIXED-HEIGHT panel — ChatView owns its own scroll + spacer, and
// relaying height across three frames invites layout thrash and feedback
// loops. HEIGHT is defined so an app that genuinely wants to size to
// content can opt in, but the runtime helper does not wire it unless
// asked. Keeping the type here (rather than inventing it ad hoc later)
// means the namespace stays closed and greppable.

// True when `event` has an explicitly allowed origin, exact source and our
// namespace/correlation id. Opaque frames report "null". `expectedSource` is the specific
// window we expect to hear from (the embed's contentWindow on the parent
// side; `window.parent` on the child side). This is a routing guard —
// callers should treat anything that fails it as not-ours and ignore it,
// NOT as an error. Server authorization never depends on this predicate.
export function isEmbedMessage(event, { origin, origins, expectedSource, instanceId }) {
  const allowedOrigins = origins || (origin ? [origin] : [])
  if (!event || !allowedOrigins.includes(event.origin)) return false
  if (expectedSource && event.source !== expectedSource) return false
  const msg = event.data
  if (!msg || typeof msg !== 'object') return false
  if (typeof msg.type !== 'string' || !msg.type.startsWith(NS)) return false
  // instanceId correlates a specific embed mount to its specific caller,
  // so two embeds opened by the same app (or one app reloaded) can't
  // cross-talk. Absent on the very first READY handshake the parent uses
  // to LEARN the instanceId? No — the parent mints the instanceId and
  // sends it in INIT, so every reply already carries it. We require it.
  if (instanceId && msg.instanceId !== instanceId) return false
  return true
}

// Event bus for the embed handle (`window.mobius.chat(...).on(event, cb)`).
// The four embed events split into two kinds:
//   - one-shot lifecycle: 'ready' and 'error' fire at most once per mount,
//     but the child posts its mount-time READY before the app (which only
//     gets the handle AFTER `await chat(...)`) can attach a listener — so a
//     handler registered "right after the await" would miss it. These are
//     made STICKY: the latest detail is recorded, and a late `on('ready'|
//     'error', cb)` replays it synchronously on registration.
//   - repeatable: 'message-sent' and 'turn-done' fire once per turn. They are
//     NOT sticky — replaying a past one to a late listener would double-fire.
// Mirrored (not imported) by mobius-runtime.js's makeChat, which can't import
// this /src module; keep the two in sync.
const STICKY_EVENTS = new Set([
  NS + 'ready',
  NS + 'error',
  'ready',
  'error',
])

export function makeEmitter() {
  const listeners = Object.create(null)
  // Latest detail for sticky events only, so a late on() can replay it.
  const lastEmit = Object.create(null)

  function emit(name, detail) {
    if (STICKY_EVENTS.has(name)) lastEmit[name] = detail
    const cbs = listeners[name]
    if (!cbs) return
    for (const cb of cbs) {
      try {
        cb(detail)
      } catch (e) {}
    }
  }

  function on(name, cb) {
    if (typeof cb !== 'function') return
    ;(listeners[name] || (listeners[name] = [])).push(cb)
    // Replay a one-shot lifecycle event that already fired, so a handler
    // attached after the embed's mount-time READY still observes it.
    if (STICKY_EVENTS.has(name) && Object.prototype.hasOwnProperty.call(lastEmit, name)) {
      try {
        cb(lastEmit[name])
      } catch (e) {}
    }
  }

  return { emit, on }
}

// Stable inert route: configuration and credentials are never serialized into
// its URL. Kept here so the helper and regression tests agree on the contract.
export function embedUrl({ base = '' } = {}) {
  return `${base}/shell/embed/chat`
}

// A refresh exchange is a two-session handoff: failure must not tear down the
// still-valid session/UI. Initial authorization has no prior authority and
// therefore remains blank and fail-closed.
export function retainEmbedSessionAfterExchangeFailure(hadAuthorizedSession) {
  return hadAuthorizedSession === true
}
