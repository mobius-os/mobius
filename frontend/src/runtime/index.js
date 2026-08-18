// Shared mini-app runtime — exposes `window.mobius` to apps running in the
// opaque app frame. Both the workspace and the installed standalone host use
// AppCanvas to mount that same frame. Imported at an absolute path
// (`/mobius-runtime.js`) from `/api/apps/{id}/frame`. It lives in `public/`,
// so Vite copies it to the build root and Workbox precaches it
// (content-revisioned per deploy → fresh online, available offline).
//
// Purpose: let offline-capable apps (offline_capable flag, Tier 3)
// persist AND read through a network outage. Writes go to /api/storage; when
// offline or the request fails, they queue in IndexedDB (the outbox) and flush
// when the connection returns. Reads are read-through: an online get() mirrors
// the value into IndexedDB so a later offline get() serves the last-known
// value (overlaid with any pending write — read-your-writes). This is the
// SAME runtime for both entry points because both mount the same opaque frame.
//
// API — intentionally small; grow it when a real app needs more. Reads/writes
// are TYPED: pick the method for your data shape (json is the default). A read
// of the wrong type for a path throws a clear error rather than corrupting:
//   window.mobius.appId
//   window.mobius.online                          -> probed reachability verdict (the shell's /api/health probe forwarded by AppCanvas; navigator.onLine is only the initial seed)
//   window.mobius.storage.get(path)               -> JSON value | null  (offline-capable, SWR)
//   window.mobius.storage.set(path, data)         -> {synced} | {queued}
//   window.mobius.storage.getText(path)           -> string | null      (offline-capable, SWR)
//   window.mobius.storage.setText(path, str, opts?)-> {synced} | {queued}   opts.contentType
//   window.mobius.storage.getBlob(path)           -> Blob | null        (offline, cache-first)
//   window.mobius.storage.setBlob(path, blob, opts?)-> {synced} | {queued}  opts.contentType; <=25 MiB
//   window.mobius.storage.remove(path)            -> {synced} | {queued}
//   window.mobius.storage.list(prefix, opts?)     -> entries[]  (offline-capable: cache+outbox overlay)
//     opts.includeContent adds `content` to small JSON file entries in the
//     server's bounded listing response; exceptional entries remain metadata-only.
//   window.mobius.storage.subscribe(path, cb)     -> unsubscribe fn (cb(json value))
//   window.mobius.storage.subscribeText(path, cb) -> unsubscribe fn (cb(string))
//   window.mobius.storage.subscribeBlob(path, cb) -> unsubscribe fn (cb(Blob); app revokes object URLs)
//   window.mobius.storage.pendingCount()          -> Promise<number>
//   window.mobius.storage.getWithVersion(path, kind?) -> {value, version}   read + its server ETag, for compare-and-swap
//   window.mobius.storage.durableWrite(path, data, opts?) -> {durability, path, writeId, version?}
//   window.mobius.runtimeFeatures.idleDocument    -> true when null/empty useDocument paths are idle
//     opts.ifMatch=version makes it a CONDITIONAL write; a 412 rejects with DurableWriteError{code:'conflict', retryable:true}.
//     CAS a file with several writers (agent + cron + UI): getWithVersion -> merge -> durableWrite({ifMatch:version}); on a
//     'conflict' error re-read + retry (the app owns its merge; the runtime does NOT retry for you). See building-apps.md.
//   window.mobius.chat({mount, chatId?, picker?, ...}) -> Promise<handle>
//     Embeds the real agent chat (ChatView) in a nested iframe inside
//     `mount`. handle.on('ready'|'message-sent'|'turn-done'|'error', cb),
//     handle.setGuidance(text), and handle.destroy(). See the
//     "Agent-chat embed" block below.
//   window.mobius.nav.open(label, onBack)        -> { ready, outcome, close }
//     outcome distinguishes host ownership from request failures; see
//     building-apps.md.
//   window.mobius.immersive.toggle() / set(hidden) -> hides/shows the Möbius top
//     bar so an app with its own header takes the full pane (no two toolbars).
//     .hidden getter, .subscribe(cb), and .holdToToggle(el) (long-press an
//     element — e.g. the app's own logo — to toggle). Sessional; standalone
//     ignores it. See src/lib/immersive.js + AppCanvas.jsx.
//   window.mobius.capabilities.available(name, version?)
//   window.mobius.capabilities.open(name, input)  -> capability session
//     session.ready, session.result, session.on(event, cb), finish(), cancel()
//   window.mobius.capabilities.invoke(name, input, {signal?}) -> one-shot result
//
// "No walls": this runtime is the easy DEFAULT, not a cage. In the default
// shell mount the app has an opaque origin, so IndexedDB/OPFS/SQLite-wasm are
// unavailable and this scoped runtime is the durable storage path. A future
// reviewed per-app-origin mode can add those APIs without restoring shell-origin
// privilege; an app may always talk to its own backend through scoped routes.
//
// Storage conflict policy: last-write-wins at the path granularity. The newest
// PUT/DELETE for a path supersedes any earlier one — enforced by coalescing
// those state operations in the outbox and routing all server writes through
// the single outbox-lock-serialized drain, so a stale queued op can never replay
// over a newer value. Signals are events rather than path state and explicitly
// do NOT coalesce. An app that needs per-record LWW stores one file per record
// (…/items/<uuid>.json) so concurrent edits to different records don't
// clobber each other. CRDTs are out of scope (overkill for single-owner
// personal apps).
//
// Smells: see the block at the bottom of this file.


import { DurableWriteError, createUseDocument, makeStorage } from './storage.js'
import { makeSignal } from './signal.js'
import { makeChat } from './chat.js'
import { makeNav, makeSplit } from './navigation.js'
import { makeCapabilities } from './capabilities.js'
import { makeImmersive } from './immersive.js'
import { tokenMatchesRuntime } from './token.js'

export * from './storage.js'
export * from './signal.js'
export * from './chat.js'
export * from './navigation.js'
export * from './capabilities.js'
export * from './immersive.js'


// ── P1-A: probed-online reactive backing ─────────────────────────────────────
// window.mobius.online returns this value (seeded from navigator.onLine).
// AppCanvas posts `moebius:online-status` whenever the trusted host's probed
// reachability verdict changes; the listener below updates _online and
// notifies subscribers.
//
// Kept in a deliberately-delimited block so concurrent worktree merges stay
// clean — edits to this runtime should land near existing connectivity code.
// ─────────────────────────────────────────────────────────────────────────────
let _online = typeof navigator !== 'undefined' ? navigator.onLine : true
const _onlineListeners = new Set()
// After AppCanvas posts its first probed verdict (on iframe load, then on every
// change) the /api/health probe is the SOLE authority for connectivity. The raw
// navigator online/offline events are only a pre-probe seed: navigator.onLine
// reads 'true' on captive portals and dead LANs, so letting it keep driving
// _online after a probe verdict has landed would let a false 'online' override a
// correct offline verdict (and the reverse) — the two drivers silently contradict.
let _probedVerdictReceived = false

function _setOnline(next) {
  if (next === _online) return
  _online = next
  for (const cb of [..._onlineListeners]) {
    try { cb(next) } catch (e) {}
  }
}

// Seed connectivity from raw browser events ONLY until the first probed verdict.
function _seedOnline(next) {
  if (_probedVerdictReceived) return
  _setOnline(next)
}

// Listen for the probed verdict from AppCanvas.
if (typeof window !== 'undefined') {
  window.addEventListener('message', (e) => {
    if (e.origin !== window.location.origin) return
    const msg = e.data
    if (!msg || typeof msg !== 'object') return
    if (msg.type === 'moebius:online-status' && typeof msg.online === 'boolean') {
      _probedVerdictReceived = true
      _setOnline(msg.online)
    }
  })
  // Pre-probe seed only (see _seedOnline): inert once a probed verdict lands.
  window.addEventListener('online', () => _seedOnline(true))
  window.addEventListener('offline', () => _seedOnline(false))
}
// ─────────────────────────────────────────────────────────────────────────────

// ── Host capability sessions ────────────────────────────────────────────────
// Opaque app frames cannot use every origin-bound browser API directly. This
// broker exposes one transport for every shell-owned operation instead of
// growing one postMessage dialect per feature.
//
// A capability is independently versioned and declared in the reviewed app
// contract. open() returns immediately so callers can cancel even while a
// browser permission prompt is pending. Every session has the same lifecycle:
// ready -> zero or more named events -> result/error, with generic controls.
let _runtimeContext = null

// App bundles embed the runtime that was present when they were compiled. A
// small explicit feature map lets an app adopt a new runtime contract without
// guessing from a platform version or breaking when an app update lands before
// the matching platform update. Additive booleans keep the check cheap and
// preserve old locally-modified app bundles: an absent key simply means the app
// should keep its legacy fallback.
export const runtimeFeatures = Object.freeze({
  idleDocument: true,
})

export function init({ appId, appInstanceId = null, getToken, capabilityContract = null }) {
  const identityKey = `${String(appId)}:${appInstanceId || 'legacy'}`
  if (_runtimeContext && _runtimeContext.identityKey === identityKey) {
    // Hosts may replace their token broker after a refresh. Keep one runtime and
    // one listener set, but route future requests through the newest function.
    _runtimeContext.tokenRef.current = getToken
    _runtimeContext.capabilities?._updateDeclarations(capabilityContract?.runtime || {})
    return _runtimeContext.api
  }
  if (_runtimeContext) {
    _runtimeContext.signal?._destroy?.()
    _runtimeContext.storage?._destroy?.()
    _runtimeContext.capabilities?._destroy?.()
  }

  const tokenRef = { current: getToken }
  const scopedToken = async (options) => {
    const token = await tokenRef.current(options)
    return tokenMatchesRuntime(token, appId, appInstanceId) ? token : null
  }
  const storage = makeStorage({
    appId,
    appInstanceId,
    getToken: scopedToken,
    isOnline: () => _online,
  })
  const signal = makeSignal(appId, storage, appInstanceId)
  const capabilities = makeCapabilities({ declarations: capabilityContract?.runtime || {} })
  const api = {
    appId,
    // Returns the probed reachability verdict (not raw navigator.onLine).
    // AppCanvas forwards the trusted host's /api/health probe result.
    get online() { return _online },
    // Subscribe to online/offline changes. `cb(boolean)` fires immediately
    // with the current value and again whenever the value changes.
    // Returns an unsubscribe function (call it on component unmount).
    onOnlineChange(cb) {
      if (typeof cb !== 'function') return () => {}
      _onlineListeners.add(cb)
      try { cb(_online) } catch (e) {}
      return () => { _onlineListeners.delete(cb) }
    },
    storage,
    DurableWriteError,
    durableWrite: storage.durableWrite,
    onDeadLetter: storage.onDeadLetter,
    runtimeFeatures,
    // useDocument is a React hook, so it must run on the APP's React instance.
    // The runtime is deliberately React-free (and headless-testable), and no
    // host sets window.React, so a self-binding window.mobius.useDocument would
    // throw. Expose the factory instead: apps bind it once at module top with
    // the React they already import — `const useDocument =
    // window.mobius.createUseDocument(React)`.
    createUseDocument: (React) => createUseDocument(storage, React),
    signal,
    capabilities,
    chat: makeChat({ appId, getToken: scopedToken, storage }),
    nav: makeNav(),
    split: makeSplit(),
    immersive: makeImmersive({ appId }),
  }
  window.mobius = api
  _runtimeContext = { identityKey, tokenRef, storage, signal, capabilities, api }
  storage._drain()    // flush anything left from a previous offline session
  storage._drainSignals() // independently flush retained telemetry
  // Ask for durable storage so the offline mirror + queued blob writes survive
  // storage pressure. The opaque frame and trusted host share the origin's
  // quota even though app code cannot access host state. Best-effort.
  try {
    if (navigator.storage && navigator.storage.persist) {
      navigator.storage.persisted().then((p) => p || navigator.storage.persist()).catch(() => {})
    }
  } catch (e) {}
  return api
}

// # Smells / notes
// - RESOLVED (2026-06-01): get() now has an offline read path via the
//   read-through cache store (mirror-on-online-read, serve-offline, overlay
//   pending writes). The old "returns null offline" smell is gone.
// - The cache mirror is owner-scoped data; it lives in the shared
//   `mobius-outbox` IndexedDB (the `cache` store). client.js wipeSwCaches on
//   logout clears `mobius-*` CacheStorage but the OUTBOX/CACHE IndexedDB is a
//   separate DB — confirm logout also deletes it (delOutboxDb handles the
//   outbox DB; the cache store rides the same DB, so it's covered).
// - AppCanvas provides the refreshable app-token broker for both workspace and
//   standalone entry points. Runtime fetches retry one 401 through
//   getToken({forceRefresh:true});
//   queued writes remain intact if refresh is temporarily offline.
// - setBlob enforces a per-blob size cap (MAX_BLOB_BYTES) BEFORE any IDB/outbox
//   write, but the read-through cache has NO total-size eviction yet. A true LRU
//   needs a lastAccessed field + index the cache store lacks (cacheGet never
//   writes on read), and write-time eviction would drop hot entries — so the
//   eviction policy is deliberately deferred (filed under .pm/083). Fine at
//   personal-app scale; revisit if a blob-heavy app pressures the origin quota.
// - list() is offline-capable (078): when the server is unreachable it derives
//   direct children from the per-path read-through cache (present=false
//   tombstones excluded, so a synced delete does NOT resurrect — the hazard a
//   cached listing blob would have had), then overlays the outbox. Same
//   online/offline contract get() has. Offline entries omit size/modified_at,
//   which only the server stat provides.
