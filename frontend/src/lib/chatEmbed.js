// Protocol for the in-app agent-chat embed (capability A, design §1).
//
// A mini-app calls `window.mobius.chat({chatId?, ...})` (mobius-runtime.js)
// which mounts a NESTED same-origin iframe at the shell embed route
// (`/shell/embed/chat?chatId=…`). That iframe renders the real ChatView
// (ChatEmbed.jsx) — a stripped-chrome RENDERER over the app-attributed
// backend chat contract, never the trust boundary (design §0b: a
// same-origin app already holds the owner JWT, so the embed adds no
// confidentiality control — enforcement lives server-side).
//
// This module is the single source of truth for the postMessage shapes
// exchanged between the app frame (parent) and the embed frame (child).
// It is bundled into the shell (ChatEmbed.jsx imports it) and mirrored —
// NOT imported — by mobius-runtime.js, which is served verbatim from
// /public and so can't import a Vite-hashed /src module. The mirror is
// deliberately tiny (the NS prefix + the source/origin guard); keep the
// two in sync the way app-frame.html ↔ AppCanvas.jsx already do.
//
// Hardening (design §1.4): three same-origin frames are in play (shell →
// app frame → embed frame), so origin alone is not enough — a sibling
// frame shares the origin. Every hop validates BOTH `e.origin ===
// expectedOrigin` AND `e.source === expectedWindow` (the specific
// contentWindow / parent we are talking to), then matches the namespaced
// type and the `instanceId` correlation token. No generic relay: only
// the enumerated message types below cross a frame boundary.

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

// Parent (app frame) → child (embed frame).
export const INIT = NS + 'init' // hand the embed its config + correlation id

// Why no height relay by default: the design (§1.2) calls for a
// FIXED-HEIGHT panel — ChatView owns its own scroll + spacer, and
// relaying height across three frames invites layout thrash and feedback
// loops. HEIGHT is defined so an app that genuinely wants to size to
// content can opt in, but the runtime helper does not wire it unless
// asked. Keeping the type here (rather than inventing it ad hoc later)
// means the namespace stays closed and greppable.

// True when `event` is a same-origin, same-source message in our
// namespace for this `instanceId`. `expectedSource` is the specific
// window we expect to hear from (the embed's contentWindow on the parent
// side; `window.parent` on the child side). This is the §1.4 guard —
// callers should treat anything that fails it as not-ours and ignore it,
// NOT as an error (other frames legitimately share the origin).
export function isEmbedMessage(event, { origin, expectedSource, instanceId }) {
  if (!event || event.origin !== origin) return false
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

// Build the embed route URL. Stable, query-only (chatId may be absent on
// lazy-create). Kept here so the helper and any test agree on the shape.
export function embedUrl({ base = '', chatId, picker } = {}) {
  const root = `${base}/shell/embed/chat`
  const params = []
  if (chatId) params.push(`chatId=${encodeURIComponent(String(chatId))}`)
  if (picker === false) params.push('picker=0')
  const qs = params.join('&')
  return qs ? `${root}?${qs}` : root
}
