// Shared mini-app runtime — exposes `window.mobius` to apps running in
// both the in-shell iframe (app-frame.html) and the standalone shell
// (routes/standalone.py). Imported at an absolute path
// (`/mobius-runtime.js`) so it resolves identically from
// `/api/apps/{id}/frame` and `/apps/{slug}/`. It lives in `public/`,
// so Vite copies it to the build root and Workbox precaches it
// (content-revisioned per deploy → fresh online, available offline).
//
// Purpose: let offline-capable apps (offline_capable flag, Tier 3)
// persist through the network outage. Writes go to /api/storage; when
// offline or the request fails, they queue in IndexedDB and flush when
// the connection returns. Reads hit the network (the service worker
// serves cached app code, not storage data).
//
// API — intentionally small; grow it when a real app needs more:
//   window.mobius.appId
//   window.mobius.online                      -> navigator.onLine
//   window.mobius.storage.get(path)           -> data | null
//   window.mobius.storage.set(path, data)     -> {synced} | {queued}
//   window.mobius.storage.remove(path)        -> {synced} | {queued}
//   window.mobius.storage.pendingCount()      -> Promise<number>
//   window.mobius.chat({mount, chatId?, ...}) -> Promise<handle>
//     Embeds the real agent chat (ChatView) in a nested iframe inside
//     `mount`. handle.on('ready'|'message-sent'|'turn-done'|'error', cb)
//     and handle.destroy(). See the "Agent-chat embed" block below.
//
// Conflict policy: last-write-wins at the path granularity. The newest
// write for a path supersedes any earlier one — enforced by coalescing
// the outbox on every write (a queued op for a path is dropped when a
// newer write for that path is enqueued OR sent directly online), so a
// stale queued op can never replay over a newer value on drain. An app
// that needs per-record LWW stores one file per record
// (…/items/<uuid>.json) so concurrent edits to different records don't
// clobber each other. CRDTs are out of scope (overkill for single-owner
// personal apps).
//
// Smells: see the block at the bottom of this file.

const DB_NAME = 'mobius-outbox'
const STORE = 'ops'

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1)
    req.onupgradeneeded = () => {
      const db = req.result
      if (!db.objectStoreNames.contains(STORE)) {
        // autoIncrement `seq` gives FIFO ordering for free. `appId` is a
        // stored field, filtered at read time (one shared DB, many apps).
        db.createObjectStore(STORE, { keyPath: 'seq', autoIncrement: true })
      }
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}

// Run `fn(store)` in one transaction. `fn` may stash a result on the
// returned object's `value`; we resolve with it on commit. Doing all
// IDB work inside the single synchronous `fn` call avoids the
// auto-close that bites when you await between operations on one tx.
async function withStore(mode, fn) {
  const db = await openDb()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, mode)
    const box = {}
    fn(tx.objectStore(STORE), box)
    tx.oncomplete = () => resolve(box.value)
    tx.onerror = () => reject(tx.error)
    tx.onabort = () => reject(tx.error)
  })
}

function makeStorage({ appId, getToken }) {
  // Drop every queued op for this app + path in one transaction, then run
  // `after(store)` (if given) inside the SAME transaction. Used to enforce
  // last-write-wins at path granularity: a newer write for a path
  // supersedes any older queued write for it, so the stale op must not
  // survive to be replayed on drain. Filtering happens in the cursor
  // because the store is keyed by `seq` (FIFO), with `appId`/`path` as
  // plain fields. Doing the purge and the follow-up add in one tx keeps
  // the coalesce atomic — no window where the path has zero ops queued.
  function purgePath(path, after) {
    return withStore('readwrite', (store) => {
      store.openCursor().onsuccess = (e) => {
        const cursor = e.target.result
        if (!cursor) {
          if (after) after(store)
          return
        }
        const v = cursor.value
        if (v.appId === appId && v.path === path) cursor.delete()
        cursor.continue()
      }
    })
  }

  // Enqueue coalesces: the newest write for a path replaces any older
  // queued writes for it, so a stale op can never clobber a newer one
  // when the queue drains. (FIFO ordering across DIFFERENT paths is
  // still preserved — drainInner walks `seq` in order.)
  function enqueue(op) {
    return purgePath(op.path, (store) => {
      store.add({ ...op, appId, ts: Date.now() })
    })
  }

  function listOps() {
    return withStore('readonly', (store, box) => {
      box.value = []
      store.openCursor().onsuccess = (e) => {
        const cursor = e.target.result
        if (!cursor) return
        if (cursor.value.appId === appId) box.value.push(cursor.value)
        cursor.continue()
      }
    })
  }

  function deleteOp(seq) {
    return withStore('readwrite', (store) => { store.delete(seq) })
  }

  // Send one queued op to the storage API. PUT and DELETE are both
  // idempotent by path, so replaying a flushed op is safe. A DELETE
  // that 404s means the file is already absent — the intended end state
  // — so we treat it as success. A 401 means the token is stale; we
  // throw 'AUTH' so the drain stops WITHOUT discarding the op (a fresh
  // token on the next trigger retries it).
  async function send(op) {
    const token = await getToken()
    const url = `/api/storage/apps/${appId}/${op.path}`
    const init = { method: op.method, headers: { Authorization: `Bearer ${token}` } }
    if (op.method === 'PUT') {
      init.headers['Content-Type'] = 'application/json'
      init.body = JSON.stringify(op.data)
    }
    const res = await fetch(url, init)   // network failure throws -> transient
    if (op.method === 'DELETE' && res.status === 404) return  // already absent
    if (res.ok) return
    // Classify so one bad op can't wedge the queue (drainInner reads
    // err.fatal): 401 auth / 408 timeout / 429 rate-limit / 5xx / network
    // are transient (keep + retry); any other 4xx is fatal (drop poison op).
    const err = new Error(`HTTP ${res.status}`)
    err.fatal = res.status >= 400 && res.status < 500 &&
      ![401, 408, 429].includes(res.status)
    throw err
  }

  async function drainInner() {
    if (!navigator.onLine) return
    const ops = await listOps()           // FIFO by seq
    for (const op of ops) {
      try {
        await send(op)
        await deleteOp(op.seq)
      } catch (e) {
        if (e && e.fatal) {
          // Poison op — a malformed/forbidden request that will never
          // succeed on replay. Drop it (dead-letter) and keep draining
          // so it can't head-of-line-block every later write forever.
          // eslint-disable-next-line no-console
          console.warn('mobius: dropping un-syncable write', op.method, op.path, e.message)
          await deleteOp(op.seq)
          continue
        }
        // Transient (offline / 5xx / 401): stop so order is preserved
        // (a later op may depend on an earlier one) and retry on the
        // next trigger. The op is NOT discarded.
        break
      }
    }
  }

  // Web Locks serializes draining across contexts (an in-shell iframe
  // and a standalone page for the same app can both be open). Falls
  // back to a plain drain where Web Locks is unavailable.
  function drain() {
    if (navigator.locks && navigator.locks.request) {
      navigator.locks.request(
        `mobius-outbox-${appId}`, { ifAvailable: true },
        async (lock) => { if (lock) await drainInner() },
      ).catch(() => {})
    } else {
      drainInner().catch(() => {})
    }
  }

  for (const ev of ['online', 'focus', 'pageshow']) {
    window.addEventListener(ev, drain)
  }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') drain()
  })

  return {
    async get(path) {
      // Reads hit the network. Offline (or on any error) returns null
      // and the app shows its own empty/offline state — we deliberately
      // do not keep a local read-mirror (a second source of truth that
      // could silently diverge from the server and the outbox).
      try {
        const token = await getToken()
        const res = await fetch(`/api/storage/apps/${appId}/${path}`, {
          headers: { Authorization: `Bearer ${token}` },
        })
        if (res.status === 404) return null
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return await res.json()
      } catch (e) {
        return null
      }
    },
    async set(path, data) {
      if (navigator.onLine) {
        try {
          await send({ method: 'PUT', path, data })
          // This direct write is newer than anything still queued for
          // the path (e.g. an offline write left over from before we
          // came online). Drop those stale ops so the next drain can't
          // replay one over the value we just wrote — the last-write-
          // wins violation this guards against.
          await purgePath(path)
          return { synced: true }
        } catch (e) { /* fall through to queue */ }
      }
      await enqueue({ method: 'PUT', path, data })
      drain()
      return { queued: true }
    },
    async remove(path) {
      if (navigator.onLine) {
        try {
          await send({ method: 'DELETE', path })
          // Same as set(): the delete just landed, so any older queued
          // write/delete for this path is stale and must not replay.
          await purgePath(path)
          return { synced: true }
        } catch (e) { /* fall through to queue */ }
      }
      await enqueue({ method: 'DELETE', path })
      drain()
      return { queued: true }
    },
    async pendingCount() {
      return (await listOps()).length
    },
    _drain: drain,
  }
}

// ── Agent-chat embed (capability A, design §1) ──────────────────────
//
// `window.mobius.chat(opts)` mounts the real ChatView (the shell's chat
// UI) inside a nested same-origin iframe at the shell embed route, so an
// app gets a live agent conversation WITHOUT reimplementing chat. The
// embed is a RENDERER, never the trust boundary (§0b): a same-origin app
// already holds the owner JWT, so enforcement is server-side.
//
// This is the PARENT side of the embed postMessage protocol. The CHILD
// side is frontend/src/components/ChatEmbed/ChatEmbed.jsx, and the shapes
// are defined once in frontend/src/lib/chatEmbed.js. mobius-runtime.js is
// served verbatim from /public and can't import that bundled /src module,
// so the few constants below are MIRRORED (not imported) — keep them in
// sync, the way app-frame.html ↔ AppCanvas.jsx already are.
const EMBED_NS = 'moebius:chat-embed:'
const EMBED_INIT = EMBED_NS + 'init'
const EMBED_READY = EMBED_NS + 'ready'
const EMBED_MESSAGE_SENT = EMBED_NS + 'message-sent'
const EMBED_TURN_DONE = EMBED_NS + 'turn-done'
const EMBED_ERROR = EMBED_NS + 'error'

let _embedSeq = 0

function makeChat({ appId, getToken }) {
  // Lazily create a chat the agent turn can be attributed to, via the
  // app-attributed backend contract (design §1.1: POST /api/chats gated
  // by get_principal so an app token is accepted and the row is stamped
  // created_by_app_id). DEPENDENCY: that gating is built by the backend
  // capability-A work; until it lands, app tokens are rejected by
  // /api/chats and this returns null (the embed then shows its no-chat
  // notice). Owner-token callers already work today.
  async function createChat(opts) {
    const token = await getToken()
    // Root-relative, same as storage above — the app frame is same-origin
    // with the shell, so /api/chats resolves regardless of the deploy
    // prefix the browser uses for the embed iframe src.
    const res = await fetch('/api/chats', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        title: opts && opts.title ? opts.title : 'App chat',
        // systemPrompt / model / provider are part of the contract the
        // backend agent is shaping (per-app system prompt is its own
        // small design, design §1.5). Forward them so they're honored
        // the moment the backend accepts them; harmless extra fields
        // until then.
        ...(opts && opts.systemPrompt ? { system_prompt: opts.systemPrompt } : {}),
        ...(opts && opts.model ? { model: opts.model } : {}),
        ...(opts && opts.provider ? { provider: opts.provider } : {}),
      }),
    })
    if (!res.ok) return null
    const data = await res.json()
    return data && data.id ? String(data.id) : null
  }

  // Open the embed in a nested iframe inside `mount` (an element the app
  // controls). Returns a handle: { chatId, instanceId, iframe, destroy,
  // on(event, cb) }. Events: 'ready' | 'message-sent' | 'turn-done' |
  // 'error', each carrying { chatId }.
  return async function chat(opts = {}) {
    const mount = opts.mount
    if (!mount || typeof mount.appendChild !== 'function') {
      throw new Error('window.mobius.chat: opts.mount must be a DOM element')
    }
    let chatId = opts.chatId ? String(opts.chatId) : await createChat(opts)
    const instanceId = `${appId}:${++_embedSeq}:${Date.now()}`
    const listeners = { ready: [], 'message-sent': [], 'turn-done': [], error: [] }
    const emit = (name, detail) => { for (const cb of listeners[name] || []) { try { cb(detail) } catch (e) {} } }

    const iframe = document.createElement('iframe')
    iframe.title = 'Agent chat'
    // Fixed-height panel (design §1.2): the app sizes `mount`; the iframe
    // fills it. We deliberately do NOT relay content height across the
    // three frames — ChatView owns its own scroll + spacer.
    iframe.style.cssText = 'width:100%;height:100%;border:0;display:block'
    // Same sandbox as the app frame so ChatView (which reads the owner
    // JWT from localStorage and hits /api/chats) works same-origin.
    iframe.setAttribute(
      'sandbox',
      'allow-scripts allow-same-origin allow-forms allow-popups allow-top-navigation-by-user-activation',
    )
    iframe.src = chatId
      ? `/shell/embed/chat?chatId=${encodeURIComponent(chatId)}`
      : '/shell/embed/chat'

    function sendInit() {
      const w = iframe.contentWindow
      if (!w) return
      w.postMessage(
        { type: EMBED_INIT, instanceId, chatId: chatId || undefined },
        window.location.origin,
      )
    }

    function onMessage(e) {
      // §1.4 hardening: three same-origin frames share this origin, so
      // origin alone is insufficient — also require the message to come
      // from THIS embed's contentWindow and carry OUR instanceId.
      if (e.origin !== window.location.origin) return
      if (e.source !== iframe.contentWindow) return
      const msg = e.data
      if (!msg || typeof msg !== 'object') return
      if (typeof msg.type !== 'string' || !msg.type.startsWith(EMBED_NS)) return
      if (msg.instanceId && msg.instanceId !== instanceId) return
      if (msg.type === EMBED_READY) {
        // The embed resolved its chatId (e.g. it was opened without one
        // and INIT carried it, or a future lazy path). Adopt it.
        if (msg.chatId) chatId = String(msg.chatId)
        emit('ready', { chatId })
      } else if (msg.type === EMBED_MESSAGE_SENT) {
        emit('message-sent', { chatId })
      } else if (msg.type === EMBED_TURN_DONE) {
        emit('turn-done', { chatId })
      } else if (msg.type === EMBED_ERROR) {
        emit('error', { chatId, error: msg.error })
      }
    }

    // Register the message listener BEFORE appending the iframe, so it's
    // live before the embed can post its mount-time READY. INIT is sent
    // on the iframe's load event, which (per the HTML spec) fires after
    // the embed document's scripts have run and its own message listener
    // is registered — so the single INIT reaches it without a race, the
    // same handshake AppCanvas ↔ app-frame.html rely on.
    window.addEventListener('message', onMessage)
    iframe.addEventListener('load', sendInit)
    mount.appendChild(iframe)

    return {
      chatId,
      instanceId,
      iframe,
      on(event, cb) {
        if (listeners[event]) listeners[event].push(cb)
        return this
      },
      destroy() {
        window.removeEventListener('message', onMessage)
        iframe.removeEventListener('load', sendInit)
        if (iframe.parentNode) iframe.parentNode.removeChild(iframe)
      },
    }
  }
}

export function init({ appId, getToken }) {
  const storage = makeStorage({ appId, getToken })
  window.mobius = {
    appId,
    get online() { return navigator.onLine },
    storage,
    chat: makeChat({ appId, getToken }),
  }
  storage._drain()    // flush anything left from a previous offline session
  return window.mobius
}

// # Smells
// - get() has no offline read path (returns null offline). Apps that
//   need to render persisted data offline must keep their own copy in
//   app state; a shell-managed read cache was cut as YAGNI until an app
//   needs it. Revisit if offline read of last-known data becomes common.
// - The standalone host passes a getToken that returns the boot-time
//   app token (or owner JWT fallback). On a long offline window the app
//   token can expire; the owner JWT fallback still authenticates as
//   owner, so the drain succeeds, but a future refinement could re-mint
//   the app token at drain time.
