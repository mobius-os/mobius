import { fetchWithAppToken } from './network.js'
import { agentViewport } from '../lib/agentViewport.js'


// ── Agent-chat embed (capability A, design §1) ──────────────────────
//
// `window.mobius.chat(opts)` mounts the real ChatView inside a nested iframe.
// The outer app sandbox propagates its opaque origin inward. Navigation is
// inert; this runtime mints a one-use server grant and transfers it in memory,
// and the child exchanges it for a short-lived exact-chat session. Neither
// frame receives the owner JWT. Window/source checks route protocol messages;
// server capability verification is the authorization boundary.
// `picker` defaults true; set picker:false for a model-locked chat with
// no model/effort/provider picker while keeping attach files + send.
//
// This is the PARENT side of the embed postMessage protocol. The CHILD
// side is frontend/src/components/ChatEmbed/ChatEmbed.jsx, and the shapes
// are defined once in frontend/src/lib/chatEmbed.js. mobius-runtime.js is
// served verbatim from /public and can't import that bundled /src module,
// so the few constants below are MIRRORED (not imported) — keep them in
// sync, the way app-frame.html ↔ AppCanvas.jsx already are.
const EMBED_NS = 'moebius:chat-embed:'
const EMBED_INIT = EMBED_NS + 'init'
const EMBED_GUIDANCE = EMBED_NS + 'guidance'
const EMBED_READY = EMBED_NS + 'ready'
const EMBED_MESSAGE_SENT = EMBED_NS + 'message-sent'
const EMBED_TURN_DONE = EMBED_NS + 'turn-done'
const EMBED_ERROR = EMBED_NS + 'error'
const EMBED_AUTH_EXPIRING = EMBED_NS + 'auth-expiring'
const EMBED_BOOTSTRAP_READY = EMBED_NS + 'bootstrap-ready'
// Context protocol — mirrored from src/lib/chatEmbed.js; keep in sync.
const EMBED_CONTEXT_REQUEST = EMBED_NS + 'context-request'
const EMBED_CONTEXT_RESPONSE = EMBED_NS + 'context-response'
const EMBED_GUIDANCE_MAX_LENGTH = 300

export function sanitizeEmbedGuidance(value) {
  if (typeof value !== 'string') return null
  const guidance = value.trim()
  return guidance ? guidance.slice(0, EMBED_GUIDANCE_MAX_LENGTH) : null
}

// The four embed handle events split into two kinds. 'ready' and 'error'
// are one-shot lifecycle events, but the child posts its mount-time READY
// before the app (which only gets the handle AFTER `await chat(...)`) can
// attach a listener — so a handler registered right after the await would
// miss it. We make those two STICKY: emit() records the latest detail and a
// late on('ready'|'error', cb) replays it synchronously. 'message-sent' and
// 'turn-done' are repeatable (once per turn) and NOT sticky — replaying a
// past one to a late listener would double-fire. This mirrors makeEmitter in
// frontend/src/lib/chatEmbed.js (served verbatim from /public, can't import
// the /src module); keep the two in sync.
// Handle events use the SHORT names ('ready' etc.) the app passes to
// handle.on() and makeChat passes to emit() — not the namespaced wire types.
const EMBED_STICKY = new Set(['ready', 'error'])

function makeEmbedEmitter() {
  // Known events only — an unknown name is ignored on both emit and on,
  // preserving the original `if (listeners[event])` guard.
  const listeners = { ready: [], 'message-sent': [], 'turn-done': [], error: [] }
  const lastEmit = {}
  function emit(name, detail) {
    if (EMBED_STICKY.has(name)) lastEmit[name] = detail
    const cbs = listeners[name]
    if (!cbs) return
    for (const cb of cbs) {
      try { cb(detail) } catch (e) {}
    }
  }
  function on(name, cb) {
    if (!listeners[name]) return
    listeners[name].push(cb)
    if (EMBED_STICKY.has(name) && hasOwn(lastEmit, name)) {
      try { cb(lastEmit[name]) } catch (e) {}
    }
  }
  return { emit, on }
}

// Hold a newly-created chat iframe at opacity 0 until the child has completed
// its authorized first commit. READY currently arrives in the same task as the
// child's React state update, so two animation frames give that update one
// complete layout/paint opportunity before the parent reveals the frame. Keep
// this in the shared runtime: every app embedding chat should get a stable
// first paint without having to reinvent an onReady cover.
export function makeEmbedFrameReveal({
  reveal,
  settle = (done) => { done() },
  scheduleFrame = (callback) => requestAnimationFrame(callback),
  cancelFrame = (id) => cancelAnimationFrame(id),
} = {}) {
  let firstFrame = null
  let secondFrame = null
  let settleCleanup = null
  let revealed = false
  let destroyed = false

  function ready(onRevealed) {
    if (destroyed || revealed) return false
    revealed = true
    firstFrame = scheduleFrame(() => {
      firstFrame = null
      secondFrame = scheduleFrame(() => {
        secondFrame = null
        if (destroyed) return
        try { if (typeof reveal === 'function') reveal() } catch (e) {}
        const finish = () => {
          if (destroyed) return
          settleCleanup = null
          try { if (typeof onRevealed === 'function') onRevealed() } catch (e) {}
        }
        try {
          settleCleanup = typeof settle === 'function' ? settle(finish) : null
        } catch (e) {
          finish()
        }
      })
    })
    return true
  }

  function destroy() {
    destroyed = true
    if (firstFrame != null) cancelFrame(firstFrame)
    if (secondFrame != null) cancelFrame(secondFrame)
    try { if (typeof settleCleanup === 'function') settleCleanup() } catch (e) {}
    firstFrame = null
    secondFrame = null
    settleCleanup = null
  }

  return { ready, destroy }
}

let _embedSeq = 0

const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj || {}, key)

export function appChatMetadataBody(
  opts = {},
  { includeProvider = true, includeOwnerVisible = false } = {},
) {
  const body = {}
  if (hasOwn(opts, 'systemPrompt')) {
    body.system_prompt = opts.systemPrompt == null ? '' : String(opts.systemPrompt)
  }
  if (hasOwn(opts, 'model')) {
    body.model = opts.model == null ? '' : String(opts.model)
  }
  if (includeProvider && hasOwn(opts, 'provider')) {
    const provider = opts.provider == null ? '' : String(opts.provider).trim()
    if (provider) body.provider = provider
  }
  // projectId scopes an embedded app chat to ONE of the app's projects
  // (feature 135): the backend stores it in agent_settings_json and points the
  // injected <app_context> at projects/<id>/. Meaningful only at create; the
  // PATCH path ignores it (AppChatPatch has no project_id), so it's harmless to
  // forward in both. Apps pair it with a per-project persist key
  // (e.g. persist: 'projects/<id>/chat_id.json') for create-once-per-project.
  if (hasOwn(opts, 'projectId')) {
    const pid = opts.projectId == null ? '' : String(opts.projectId).trim()
    if (pid) body.project_id = pid
  }
  if (hasOwn(opts, 'scope')) {
    const scope = opts.scope == null ? '' : String(opts.scope).trim()
    if (scope) body.scope = scope
  }
  if (hasOwn(opts, 'scopeLabel')) {
    const label = opts.scopeLabel == null ? '' : String(opts.scopeLabel).trim()
    if (label) body.scope_label = label
  }
  if (includeOwnerVisible && hasOwn(opts, 'ownerVisible')) {
    body.owner_visible = opts.ownerVisible === true
  }
  return body
}

// Coordinates one-use bootstrap grants for both initial authorization and
// session refresh. Every retry mints a NEW grant: an exchange response can be
// lost after the server consumes the previous grant, so replaying it is never
// a recovery strategy. Delays are bounded; retries continue until success or
// the frame is replaced/destroyed.
export function makeEmbedAuthorizationHandoff({
  mint,
  post,
  onAttemptError = () => {},
  setTimer = setTimeout,
  clearTimer = clearTimeout,
  retryDelays = [250, 1000, 3000, 5000],
  acknowledgementTimeoutMs = 5000,
  makeAuthorizationId = () => (
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `authorization-${Date.now()}-${Math.random().toString(36).slice(2)}`
  ),
}) {
  let destroyed = false
  let pending = null
  let retryTimer = null
  let retryIndex = 0

  function clearPending() {
    if (pending && pending.timer != null) clearTimer(pending.timer)
    pending = null
  }

  function scheduleRetry() {
    if (destroyed || retryTimer != null) return
    const delays = retryDelays.length ? retryDelays : [1000]
    const delay = delays[Math.min(retryIndex, delays.length - 1)]
    retryIndex += 1
    retryTimer = setTimer(() => {
      retryTimer = null
      attempt()
    }, delay)
  }

  function failCurrent(authorizationId, error) {
    if (destroyed || !pending || pending.id !== authorizationId) return false
    clearPending()
    try { onAttemptError(error) } catch (e) {}
    scheduleRetry()
    return true
  }

  async function attempt() {
    if (destroyed || pending || retryTimer != null) return
    const authorizationId = makeAuthorizationId()
    const marker = { id: authorizationId, timer: null }
    pending = marker
    let capability
    try {
      capability = await mint()
    } catch (error) {
      failCurrent(authorizationId, error)
      return
    }
    if (destroyed || pending !== marker) return
    try {
      post({ authorizationId, capability })
    } catch (error) {
      failCurrent(authorizationId, error)
      return
    }
    marker.timer = setTimer(() => {
      failCurrent(authorizationId, new Error('embedded-chat authorization timed out'))
    }, acknowledgementTimeoutMs)
  }

  return {
    start() { attempt() },
    refresh() { attempt() },
    ready(authorizationId) {
      if (destroyed || !pending || pending.id !== authorizationId) return false
      clearPending()
      if (retryTimer != null) clearTimer(retryTimer)
      retryTimer = null
      retryIndex = 0
      return true
    },
    failed(authorizationId, error) {
      return failCurrent(authorizationId, error)
    },
    destroy() {
      destroyed = true
      clearPending()
      if (retryTimer != null) clearTimer(retryTimer)
      retryTimer = null
    },
  }
}

// How long a freshly mounted embed frame may stay completely silent before
// makeChat reports it dead. The child's very first message (BOOTSTRAP_READY)
// normally arrives within a couple of seconds of the document load; 15s
// tolerates a cold cache on a slow link while still bounding the
// silent-black-panel failure — a frame whose document never executes posts
// nothing, fires no 'error', and otherwise leaves apps staring at an
// invisible (opacity:0) embed forever. Observed in the field when an edge
// proxy stamped `frame-ancestors 'self'`/X-Frame-Options onto the embed
// route: its ancestor app frame is intentionally opaque, so the browser
// refused the document without any signal the parent could hear.
// Overridable per mount via opts.bootstrapTimeoutMs (tests, unusual hosts).
const EMBED_BOOTSTRAP_TIMEOUT_MS = 15000

export function makeChat({ appId, getToken, storage }) {
  // Lazily create a chat the agent turn can be attributed to, via the
  // app-attributed backend contract (design §1.1: POST /api/app-chats).
  // The ordinary /api/chats create route is owner-only and intentionally
  // leaves created_by_app_id NULL.
  // Hosts now expose one refreshable app-token broker. Keep app-chat on that
  // same authority instead of trying to mint an app token with another app
  // token (the owner-only mint endpoint correctly rejects that with 403).
  async function appChatFetch(url, init = {}) {
    return fetchWithAppToken(getToken, url, init)
  }

  async function listChats(opts = {}) {
    const scope = opts.scope == null ? '' : String(opts.scope).trim()
    const qs = scope ? `?scope=${encodeURIComponent(scope)}` : ''
    const res = await appChatFetch(`/api/app-chats${qs}`)
    if (!res.ok) {
      throw new Error(`window.mobius.chat: list failed (${res.status})`)
    }
    const data = await res.json()
    return Array.isArray(data) ? data : []
  }

  async function createChat(opts) {
    // Root-relative, same host as storage above. The app document has an
    // opaque effective origin, but its scoped bearer authorizes this request.
    const res = await appChatFetch('/api/app-chats', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        title: opts && opts.title ? opts.title : 'App chat',
        // systemPrompt / model / provider are part of the contract the
        // backend agent is shaping (per-app system prompt is its own
        // small design, design §1.5). Forward them so they're honored
        // the moment the backend accepts them; harmless extra fields
        // until then.
        ...appChatMetadataBody(opts, {
          includeProvider: true,
          includeOwnerVisible: true,
        }),
      }),
    })
    if (!res.ok) {
      throw new Error(`window.mobius.chat: create failed (${res.status})`)
    }
    const data = await res.json()
    if (!data || !data.id) {
      throw new Error('window.mobius.chat: create failed (missing chat id)')
    }
    return String(data.id)
  }

  async function updateChat(chatId, opts) {
    if (!chatId || !opts) return
    const body = {}
    Object.assign(body, appChatMetadataBody(opts, { includeProvider: false }))
    if (!Object.keys(body).length) return
    const res = await appChatFetch(`/api/app-chats/${encodeURIComponent(chatId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) {
      throw new Error(`window.mobius.chat: update failed (${res.status})`)
    }
  }

  async function mintEmbedCapability(chatId, instanceId) {
    const res = await appChatFetch(
      `/api/app-chats/${encodeURIComponent(chatId)}/embed-capability`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instance_id: instanceId }),
      },
    )
    if (!res.ok) {
      throw new Error(`window.mobius.chat: authorize failed (${res.status})`)
    }
    const data = await res.json()
    if (!data || typeof data.capability !== 'string') {
      throw new Error('window.mobius.chat: authorize failed (missing capability)')
    }
    return data.capability
  }

  async function revokeEmbed(chatId, instanceId) {
    if (!chatId || !instanceId) return
    try {
      await appChatFetch(
        `/api/app-chats/${encodeURIComponent(chatId)}/embed-sessions/${encodeURIComponent(instanceId)}`,
        { method: 'DELETE' },
      )
    } catch (e) {}
  }

  // Start a first-class owner-visible chat and submit its first turn without
  // depending on shell navigation, retained ChatView state, or browser draft
  // storage. Navigation is deliberately left to the caller: after this
  // promise resolves, an app can post moebius:open-chat for the returned id.
  async function startChat(opts = {}) {
    const content = String(opts.content ?? opts.draft ?? '').trim()
    if (!content) {
      throw new Error('window.mobius.chat.start: opts.draft must not be empty')
    }
    const chatId = await createChat({
      ...opts,
      ownerVisible: opts.ownerVisible !== false,
    })
    const cid = (
      typeof crypto !== 'undefined'
      && typeof crypto.randomUUID === 'function'
    )
      ? crypto.randomUUID()
      : `cid-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
    let timezone = 'UTC'
    try {
      timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
    } catch (e) {}
    const body = { content, cid, timezone }
    if (typeof window !== 'undefined') {
      body.viewport = agentViewport(window)
    }
    const res = await appChatFetch(
      `/api/chats/${encodeURIComponent(chatId)}/messages`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      },
    )
    if (!res.ok) {
      const error = new Error(`window.mobius.chat.start: send failed (${res.status})`)
      error.chatId = chatId
      throw error
    }
    let response = null
    try { response = await res.json() } catch (e) {}
    return { chatId, response }
  }

  // Open the embed in a nested iframe inside `mount` (an element the app
  // controls). Returns a handle: { chatId, instanceId, iframe, destroy,
  // on(event, cb) }. Events: 'ready' | 'message-sent' | 'turn-done' |
  // 'error', each carrying { chatId }.
  //
  // The helper owns the WHOLE app-chat lifecycle so apps don't hand-roll it:
  //   - `persist: '<storage-key>'` — create the app-chat once, save its id to
  //     that storage path, and REUSE it on every later mount (PATCHing the
  //     prompt on resume). Without it, an explicit `chatId` is used as-is, or
  //     an ephemeral chat is created (the original behavior).
  //   - `systemPrompt` / `title` / `model` / `provider` — shape the chat on
  //     create and re-apply (PATCH) on resume.
  //   - `onReady` / `onTurnDone` / `onMessageSent` / `onError` — handlers wired
  //     BEFORE the iframe mounts, so they never miss the mount-time READY.
  // So the common app usage is one call:
  //   const h = await window.mobius.chat({ mount, persist: 'chat_id.json',
  //     systemPrompt, picker: false, onTurnDone: refresh })  // h.destroy() on unmount
  const chat = async function chat(opts = {}) {
    const mount = opts.mount
    if (!mount || typeof mount.appendChild !== 'function') {
      throw new Error('window.mobius.chat: opts.mount must be a DOM element')
    }
    // `persist` lets the helper own create-once-then-reuse. The id is stored as
    // `{ id }` (the shape apps already wrote to chat_id.json); we also accept a
    // bare string or `{ chatId }` on read for tolerance.
    const persistKey = typeof opts.persist === 'string' && opts.persist ? opts.persist : null
    async function loadPersistedId() {
      if (!persistKey || !storage) return null
      try {
        const saved = await storage.get(persistKey)
        const id = saved && (typeof saved === 'string' ? saved : (saved.id || saved.chatId))
        return id ? String(id) : null
      } catch (e) { return null }
    }
    function savePersistedId(id) {
      if (!persistKey || !storage || !id) return
      try { Promise.resolve(storage.set(persistKey, { id: String(id) })).catch(() => {}) } catch (e) {}
    }
    // Explicit chatId wins; else a persisted id (PATCH its prompt on resume);
    // else create one and persist it. With no persist + no chatId this is the
    // original "ephemeral chat" path.
    let chatId = opts.chatId ? String(opts.chatId) : await loadPersistedId()
    const fromPersist = !opts.chatId && !!chatId
    if (chatId) {
      try {
        await updateChat(chatId, opts)
      } catch (e) {
        const emsg = String(e && e.message)
        // A persisted chat id can go stale: the empty-chat sweeper purges an
        // app-chat that never got a turn past its grace window, but the
        // persisted id (chat_id.json) still points at it, so the resume PATCH
        // 404s. Self-heal by dropping the dead id and creating a fresh chat —
        // only for a persisted id; an explicit caller-supplied chatId surfaces
        // the error (the caller named a specific chat and should hear it's gone).
        const dead = fromPersist && /\((?:404|410)\)/.test(emsg)
        if (dead) {
          chatId = await createChat(opts)
          savePersistedId(chatId)
        } else if (/\(409\)/.test(emsg)) {
          // The chat already had its first turn, so its system prompt/provider
          // are now immutable ("chat-stable app prompts"). The resume PATCH
          // re-applies opts.systemPrompt every mount; on a started chat that is
          // a benign no-op the backend rejects with 409. Swallow it and keep
          // using the existing chat — surfacing it stranded Web Studio / Workout
          // on "update failed (409)" the moment their chat had any history.
        } else {
          throw e
        }
      }
    } else {
      chatId = await createChat(opts)
      savePersistedId(chatId)
    }
    const pickerOn = opts.picker !== false
    const scopeValue = hasOwn(opts, 'scope') && opts.scope != null
      ? String(opts.scope).trim()
      : ''
    const controlsOn = opts.controls === true || (opts.controls !== false && !!scopeValue)
    let instanceId = `${appId}:${++_embedSeq}:${Date.now()}`
    let hasAuthorizedOnce = false
    let authorizationHandoff = null
    // Sticky 'ready'/'error' so a handler attached after `await chat(...)`
    // still observes the embed's mount-time READY (see makeEmbedEmitter).
    const { emit, on: onEvent } = makeEmbedEmitter()
    // opts handlers register before mount → they never miss the early READY.
    if (typeof opts.onReady === 'function') onEvent('ready', opts.onReady)
    if (typeof opts.onTurnDone === 'function') onEvent('turn-done', opts.onTurnDone)
    if (typeof opts.onMessageSent === 'function') onEvent('message-sent', opts.onMessageSent)
    if (typeof opts.onError === 'function') onEvent('error', opts.onError)

    function embedSrcFor() {
      return '/shell/embed/chat'
    }

    function createEmbedFrame() {
      const frame = document.createElement('iframe')
      frame.title = 'Agent chat'
      const reduceMotion = typeof window.matchMedia === 'function'
        && window.matchMedia('(prefers-reduced-motion: reduce)').matches
      frame.style.cssText = (
        'width:100%;height:100%;border:0;display:block;opacity:0;pointer-events:none;'
        + (reduceMotion ? '' : 'transition:opacity 120ms ease;')
      )
      // The outer app sandbox already makes every descendant opaque. Keep the
      // nested declaration equally explicit: this document never regains shell
      // origin storage/JWT authority.
      frame.setAttribute(
        'sandbox',
        'allow-scripts allow-forms allow-popups allow-top-navigation-by-user-activation',
      )
      frame.src = embedSrcFor()
      return frame
    }
    let iframe = createEmbedFrame()

    function makeFrameRevealController(targetFrame) {
      const animated = !!targetFrame.style.transition
      return makeEmbedFrameReveal({
        reveal: () => { targetFrame.style.opacity = '1' },
        settle: (done) => {
          if (!animated) {
            targetFrame.style.pointerEvents = 'auto'
            done()
            return undefined
          }
          let finished = false
          let timer = null
          const finish = () => {
            if (finished) return
            finished = true
            targetFrame.removeEventListener('transitionend', onTransitionEnd)
            if (timer != null) clearTimeout(timer)
            targetFrame.style.pointerEvents = 'auto'
            done()
          }
          const onTransitionEnd = (event) => {
            if (event.target === targetFrame && event.propertyName === 'opacity') finish()
          }
          targetFrame.addEventListener('transitionend', onTransitionEnd)
          // A detached/backgrounded frame may not dispatch transitionend.
          // Keep visual-ready bounded rather than stranding an app cover.
          timer = setTimeout(finish, 180)
          return () => {
            finished = true
            targetFrame.removeEventListener('transitionend', onTransitionEnd)
            if (timer != null) clearTimeout(timer)
          }
        },
      })
    }

    let frameReveal = makeFrameRevealController(iframe)

    // Watchdog for a frame that can never boot. Every failure PAST first
    // contact has a reporter (authorization attempts emit 'error'; the child
    // posts EMBED_ERROR for load/stream problems) — but a document that never
    // executes at all posts nothing, and the panel stays silently blank.
    // Report-only: the frame is left in place, so a genuinely slow boot that
    // completes after the deadline still authorizes and fires 'ready'
    // normally; 'error' is sticky, so a handler attached later still
    // observes it.
    const bootstrapTimeoutMs = (
      Number.isFinite(opts.bootstrapTimeoutMs) && opts.bootstrapTimeoutMs > 0
    ) ? opts.bootstrapTimeoutMs : EMBED_BOOTSTRAP_TIMEOUT_MS
    let bootstrapTimer = null
    function disarmBootstrapWatchdog() {
      if (bootstrapTimer != null) {
        clearTimeout(bootstrapTimer)
        bootstrapTimer = null
      }
    }
    function armBootstrapWatchdog() {
      disarmBootstrapWatchdog()
      const watchedFrame = iframe
      bootstrapTimer = setTimeout(() => {
        bootstrapTimer = null
        if (watchedFrame !== iframe) return
        emit('error', {
          chatId,
          error: 'embedded chat did not start (its frame was blocked or failed to load)',
        })
      }, bootstrapTimeoutMs)
    }

    // Keep the older prompt-chip contract for apps that still use it. Newer
    // apps can provide one calm guidance line instead; guidance wins in the
    // renderer when both are present.
    const quickActions = Array.isArray(opts.quickActions)
      ? opts.quickActions
          .filter(a => a && typeof a.label === 'string' && typeof a.prompt === 'string')
          .slice(0, 4)
      : undefined
    let guidance = sanitizeEmbedGuidance(opts.guidance)

    let controlsShell = null
    let frameMount = mount
    let selectEl = null
    let newChatButton = null
    let onChatSelectChange = null
    let onNewChatClick = null

    function errorText(err) {
      return err && err.message ? err.message : String(err || 'Unknown error')
    }

    function chatOptionLabel(chat) {
      const label = chat && typeof chat.scope_label === 'string' ? chat.scope_label.trim() : ''
      const title = chat && typeof chat.title === 'string' ? chat.title.trim() : ''
      if (label) return label
      if (title) return title
      return chat && chat.id ? `Chat ${String(chat.id).slice(0, 8)}` : 'Chat'
    }

    function renderChatOptions(chats) {
      if (!selectEl) return
      const options = []
      const seen = new Set()
      for (const chat of chats || []) {
        if (!chat || !chat.id) continue
        const id = String(chat.id)
        if (seen.has(id)) continue
        seen.add(id)
        options.push({ ...chat, id })
      }
      if (chatId && !seen.has(chatId)) {
        options.unshift({
          id: chatId,
          title: opts.title || 'Current chat',
          scope_label: opts.scopeLabel || opts.title || 'Current chat',
        })
      }
      selectEl.replaceChildren(...options.map((chat) => {
        const option = document.createElement('option')
        option.value = chat.id
        option.textContent = chatOptionLabel(chat)
        return option
      }))
      selectEl.value = chatId || ''
    }

    async function refreshChatOptions() {
      if (!controlsOn || !selectEl) return
      try {
        renderChatOptions(await listChats(opts))
      } catch (e) {
        emit('error', { chatId, error: errorText(e) })
      }
    }

    async function switchToChat(nextId) {
      nextId = nextId ? String(nextId) : ''
      if (!nextId || nextId === chatId) return
      const previousId = chatId
      if (selectEl) selectEl.disabled = true
      try {
        await updateChat(nextId, opts)
        const nextInstanceId = `${appId}:${++_embedSeq}:${Date.now()}`
        await revokeEmbed(previousId, instanceId)
        chatId = nextId
        instanceId = nextInstanceId
        savePersistedId(chatId)
        replaceEmbedFrame()
      } catch (e) {
        if (selectEl) selectEl.value = previousId || ''
        emit('error', { chatId: previousId, error: errorText(e) })
      } finally {
        if (selectEl) selectEl.disabled = false
      }
    }

    async function startNewChat() {
      if (newChatButton) newChatButton.disabled = true
      try {
        const previousId = chatId
        const previousInstanceId = instanceId
        const nextId = await createChat(opts)
        const nextInstanceId = `${appId}:${++_embedSeq}:${Date.now()}`
        await revokeEmbed(previousId, previousInstanceId)
        chatId = nextId
        instanceId = nextInstanceId
        savePersistedId(chatId)
        replaceEmbedFrame()
        await refreshChatOptions()
        if (selectEl) selectEl.value = chatId
      } catch (e) {
        emit('error', { chatId, error: errorText(e) })
      } finally {
        if (newChatButton) newChatButton.disabled = false
      }
    }

    if (controlsOn) {
      controlsShell = document.createElement('div')
      controlsShell.style.cssText = (
        'width:100%;height:100%;min-height:0;display:flex;flex-direction:column;'
      )
      const chrome = document.createElement('div')
      chrome.style.cssText = (
        'display:flex;align-items:center;gap:6px;flex:0 0 auto;'
        + 'padding:6px 8px;border-bottom:1px solid rgba(148,163,184,.28);'
        + 'background:rgba(248,250,252,.94);'
      )
      selectEl = document.createElement('select')
      selectEl.setAttribute('aria-label', 'Chat')
      selectEl.style.cssText = (
        'min-width:0;flex:1 1 auto;height:28px;border:1px solid rgba(148,163,184,.55);'
        + 'border-radius:6px;background:#fff;color:#111827;font:500 12px system-ui,sans-serif;'
        + 'padding:0 26px 0 8px;'
      )
      newChatButton = document.createElement('button')
      newChatButton.type = 'button'
      newChatButton.textContent = '+'
      newChatButton.title = 'New chat'
      newChatButton.setAttribute('aria-label', 'New chat')
      newChatButton.style.cssText = (
        'width:28px;height:28px;flex:0 0 28px;border:1px solid rgba(148,163,184,.55);'
        + 'border-radius:6px;background:#fff;color:#111827;font:600 18px/1 system-ui,sans-serif;'
        + 'display:grid;place-items:center;cursor:pointer;'
      )
      onChatSelectChange = () => { switchToChat(selectEl.value).catch(() => {}) }
      onNewChatClick = () => { startNewChat().catch(() => {}) }
      selectEl.addEventListener('change', onChatSelectChange)
      newChatButton.addEventListener('click', onNewChatClick)
      chrome.appendChild(selectEl)
      chrome.appendChild(newChatButton)
      frameMount = document.createElement('div')
      frameMount.style.cssText = 'min-height:0;flex:1 1 auto;'
      controlsShell.appendChild(chrome)
      controlsShell.appendChild(frameMount)
      renderChatOptions([])
    }

    function createAuthorizationHandoff() {
      const targetFrame = iframe
      const targetChatId = chatId
      const targetInstanceId = instanceId
      return makeEmbedAuthorizationHandoff({
        mint: () => mintEmbedCapability(targetChatId, targetInstanceId),
        post: ({ authorizationId, capability }) => {
          if (
            targetFrame !== iframe
            || targetChatId !== chatId
            || targetInstanceId !== instanceId
            || !targetFrame.contentWindow
          ) throw new Error('embedded-chat frame was replaced')
          // The bearer is transferred only in memory. `*` is required for an
          // opaque target; source/instance/authorization ids are routing guards,
          // while the one-use server exchange is the authorization boundary.
          const msg = {
            type: EMBED_INIT,
            instanceId: targetInstanceId,
            chatId: targetChatId,
            authorizationId,
            bootstrapCapability: capability,
            picker: pickerOn,
          }
          if (quickActions && quickActions.length > 0) msg.quickActions = quickActions
          if (guidance) msg.guidance = guidance
          targetFrame.contentWindow.postMessage(msg, '*')
        },
        onAttemptError: (error) => {
          emit('error', { chatId: targetChatId, error: errorText(error) })
        },
      })
    }

    function replaceEmbedFrame() {
      const previous = iframe
      authorizationHandoff?.destroy()
      frameReveal?.destroy()
      iframe = createEmbedFrame()
      frameReveal = makeFrameRevealController(iframe)
      hasAuthorizedOnce = false
      authorizationHandoff = createAuthorizationHandoff()
      if (previous.parentNode) previous.parentNode.replaceChild(iframe, previous)
      armBootstrapWatchdog()
    }

    function postGuidance() {
      const targetFrame = iframe
      if (!targetFrame.contentWindow) return
      targetFrame.contentWindow.postMessage({
        type: EMBED_GUIDANCE,
        instanceId,
        chatId,
        guidance,
      }, '*')
    }

    function onMessage(e) {
      if (e.origin !== 'null' && e.origin !== window.location.origin) return
      if (e.source !== iframe.contentWindow) return
      const msg = e.data
      if (!msg || typeof msg !== 'object') return
      if (typeof msg.type !== 'string' || !msg.type.startsWith(EMBED_NS)) return
      // Any authentic embed message from the current frame (the source check
      // above) proves its document booted — the watchdog's only question.
      disarmBootstrapWatchdog()
      // A lazy route can finish document loading before its React effect has
      // installed the INIT listener. Mint the one-use grant only after the
      // exact child WindowProxy says that receiver is ready.
      if (msg.type === EMBED_BOOTSTRAP_READY) {
        authorizationHandoff?.start()
        return
      }
      if (msg.instanceId !== instanceId) return
      if (msg.type === EMBED_READY) {
        if (!authorizationHandoff?.ready(msg.authorizationId)) return
        // Adopt and persist the chat only after the server-authorized exchange
        // for this exact attempt succeeded.
        if (msg.chatId) {
          const resolved = String(msg.chatId)
          if (resolved !== chatId) { chatId = resolved; savePersistedId(chatId) }
        }
        if (!hasAuthorizedOnce) {
          hasAuthorizedOnce = true
          // INIT may have been posted before the caller received its handle.
          // Re-send the current value at READY so a setGuidance() during the
          // authorization exchange cannot be stranded behind the old INIT.
          postGuidance()
          // `ready` is now visually truthful: callers receive it only after
          // the authorized child has had two frames to commit and the iframe
          // itself is revealed. Authorization acknowledgement above remains
          // immediate, so the one-use handoff timeout is never held open by UI.
          frameReveal.ready(() => emit('ready', { chatId }))
        }
      } else if (msg.type === EMBED_MESSAGE_SENT) {
        emit('message-sent', { chatId })
      } else if (msg.type === EMBED_TURN_DONE) {
        emit('turn-done', { chatId })
      } else if (msg.type === EMBED_ERROR && msg.phase === 'authorization') {
        authorizationHandoff?.failed(
          msg.authorizationId,
          new Error(msg.error || 'embedded-chat authorization failed'),
        )
      } else if (msg.type === EMBED_ERROR) {
        emit('error', { chatId, error: msg.error })
      } else if (msg.type === EMBED_AUTH_EXPIRING) {
        authorizationHandoff?.refresh()
      } else if (msg.type === EMBED_CONTEXT_REQUEST) {
        // The child is asking for current app state before submitting a message.
        // Call opts.getContext() if provided; reply even if absent (nonce
        // correlation lets the child match the response to its pending request).
        const nonce = msg.nonce
        const getContext = typeof opts.getContext === 'function' ? opts.getContext : null
        Promise.resolve(getContext ? getContext() : null).then((ctx) => {
          const w = iframe.contentWindow
          if (!w) return
          w.postMessage(
            { type: EMBED_CONTEXT_RESPONSE, instanceId, nonce, context: ctx || null },
            '*',
          )
        }).catch(() => {
          const w = iframe.contentWindow
          if (!w) return
          w.postMessage(
            { type: EMBED_CONTEXT_RESPONSE, instanceId, nonce, context: null },
            '*',
          )
        })
      }
    }

    // Register before append so the child's bootstrap-ready message cannot be
    // lost. The child sends it only after installing its INIT listener.
    authorizationHandoff = createAuthorizationHandoff()
    window.addEventListener('message', onMessage)
    frameMount.appendChild(iframe)
    armBootstrapWatchdog()
    if (controlsShell) {
      mount.appendChild(controlsShell)
      refreshChatOptions().catch(() => {})
    }

    return {
      get chatId() { return chatId },
      get instanceId() { return instanceId },
      get iframe() { return iframe },
      on(event, cb) {
        // Delegates to the sticky emitter: a 'ready'/'error' that already
        // fired (the mount-time READY) replays to a late handler.
        onEvent(event, cb)
        return this
      },
      setGuidance(value) {
        const nextGuidance = sanitizeEmbedGuidance(value)
        if (nextGuidance === guidance) return this
        guidance = nextGuidance
        // Before READY, the latest value travels with INIT. Afterwards, update
        // the authorized child in place so changing app context never remounts
        // the iframe or interrupts a streaming turn.
        if (hasAuthorizedOnce) postGuidance()
        return this
      },
      destroy() {
        window.removeEventListener('message', onMessage)
        disarmBootstrapWatchdog()
        authorizationHandoff?.destroy()
        frameReveal?.destroy()
        revokeEmbed(chatId, instanceId)
        if (selectEl && onChatSelectChange) {
          selectEl.removeEventListener('change', onChatSelectChange)
        }
        if (newChatButton && onNewChatClick) {
          newChatButton.removeEventListener('click', onNewChatClick)
        }
        if (controlsShell && controlsShell.parentNode) {
          controlsShell.parentNode.removeChild(controlsShell)
        } else if (iframe.parentNode) {
          iframe.parentNode.removeChild(iframe)
        }
      },
    }
  }
  chat.start = startChat
  return chat
}
