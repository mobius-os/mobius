import { useCallback, useEffect, useRef, useState } from 'react'
import ChatView from '../ChatView/ChatView.jsx'
import ErrorBoundary from '../ErrorBoundary/ErrorBoundary.jsx'
import useTheme from '../../hooks/useTheme.js'
import {
  INIT, READY, MESSAGE_SENT, TURN_DONE, ERROR,
  CONTEXT_REQUEST, CONTEXT_RESPONSE,
  isEmbedMessage,
} from '../../lib/chatEmbed.js'
import {
  EMPTY_TURN_DONE_GATE,
  advanceTurnDoneGate,
} from '../../lib/chatRunSignal.js'
import './ChatEmbed.css'

// Stripped-chrome agent-chat embed (capability A, design §1).
//
// This is the RENDERER in the embed-as-renderer design: a SEPARATE
// entry (NOT conditionals threaded through Shell) that mounts the real
// ChatView scoped to one chatId, in the shell's own React-19 +
// QueryClient context. App.jsx wraps both this and the full Shell in the
// same PersistQueryClientProvider, so ChatView's useQueryClient resolves
// and its cache (messages, theme) is shared.
//
// It is NOT the trust boundary (design §0b): a same-origin app already
// holds the owner JWT, so the embed adds no confidentiality control —
// the real authority is the app-attributed backend chat contract. Do not
// add auth gating here.
//
// Frame layering is shell → app frame → embed frame, all same-origin, so
// every postMessage is validated by source + origin + instanceId
// (lib/chatEmbed.js). The runtime helper (mobius-runtime.js) is the
// parent: it resolves a chatId (lazy-creating one via the backend
// contract when the app didn't pass one), opens this iframe at
// `/shell/embed/chat?chatId=…`, and relays the lifecycle messages we
// post here back to the app.
//
// ChatView is a pure RENDERER over an existing chat — it POSTs to
// /api/chats/{chatId}/messages and never creates a chat. So this embed
// always needs a resolved chatId; lazy-create lives in the runtime
// helper, not here. Without one we render a read-only "no chat" notice
// rather than ChatView's empty-new-chat composer (which would fail to
// send with no id to POST to).
//
// REMOUNT IS NORMAL. The parent app frame can be evicted (Shell's app
// LRU), reloaded (app_updated key change, shell rebuild, BFCache), or
// the embed iframe re-created — any of which remounts this component
// with a fresh document. We rely on ChatView's durable-chat contract:
// it reads chatId from props, refetches /api/chats/{id} on mount, and
// reconnects the SSE catch-up burst if a turn is in flight. So a remount
// transparently reloads history and rejoins a live turn — no extra state
// to persist. We just re-announce READY so the parent can re-correlate.

function readChatIdFromUrl() {
  try {
    return new URLSearchParams(window.location.search).get('chatId') || null
  } catch {
    return null
  }
}

function readPickerFromUrl() {
  try {
    return new URLSearchParams(window.location.search).get('picker') !== '0'
  } catch {
    return true
  }
}

export default function ChatEmbed() {
  // Self-theme on mount. The embed branch (App.jsx) renders OUTSIDE
  // Shell, which is the only other useTheme() caller — so without this
  // the embed inherits no theme and paints with the unstyled default
  // tokens (black-on-black in dark, black composer in light). useTheme
  // reads the persisted theme via React Query (hydrated from IndexedDB,
  // refetched from /api/theme) and runs applyThemeToDom, so the embed
  // matches the owner's theme in BOTH modes and live-updates when the
  // theme query is invalidated (e.g. agent ships a new theme.css). The
  // server already injects the initial theme block into the served HTML;
  // this is what keeps the embed correct when the SW serves the
  // non-injected precache, and what makes light/dark toggles propagate.
  useTheme()
  // chatId is normally fixed for the life of this document (the runtime
  // helper navigates the iframe to change it, which remounts us). INIT
  // may still supply one if the helper opened us before lazy-create
  // resolved, so keep it in state.
  const [chatId, setChatId] = useState(() => readChatIdFromUrl())
  const [picker, setPicker] = useState(() => readPickerFromUrl())
  // quickActions: array of {label, prompt} from the INIT payload. Max 4.
  // Passed to ChatView which renders them as chips on the embedded empty state.
  const [quickActions, setQuickActions] = useState(null)

  // The correlation token the parent (app frame) minted in INIT. Until
  // it arrives our outbound messages omit instanceId; the parent's
  // isEmbedMessage guard treats those as not-yet-correlated. In practice
  // INIT lands within the first paint. Null is the honest "not
  // correlated yet" value.
  const instanceIdRef = useRef(null)
  // The window we talk to. Our parent is the app frame (or the shell on a
  // top-level open). Either way it is `window.parent` and same-origin;
  // when there is no parent (opened standalone) parent === window.
  const parentRef = useRef(typeof window !== 'undefined' ? window.parent : null)

  // Keep postToParent's closure reading the latest chatId without
  // re-subscribing the listener.
  const chatIdRef = useRef(chatId)
  chatIdRef.current = chatId
  // TURN_DONE can arrive from the per-chat stream or the process-wide run
  // signal. Arm once per turn and let the first terminal path win, so cross-SSE
  // delivery order can never post the parent protocol message twice.
  const turnDoneGateRef = useRef(EMPTY_TURN_DONE_GATE)
  const armTurnDone = useCallback(() => {
    turnDoneGateRef.current = advanceTurnDoneGate(
      turnDoneGateRef.current,
      'message_started',
    ).gate
  }, [])
  const notifyTurnDone = useCallback(({ continues = false } = {}) => {
    const result = advanceTurnDoneGate(
      turnDoneGateRef.current,
      continues ? 'stream_continues' : 'stream_finished',
    )
    turnDoneGateRef.current = result.gate
    if (!result.emit) return
    postToParent(TURN_DONE)
  }, [postToParent])
  const handleExternalRunEvent = useCallback((eventType) => {
    const result = advanceTurnDoneGate(turnDoneGateRef.current, eventType)
    turnDoneGateRef.current = result.gate
    if (result.emit) postToParent(TURN_DONE)
  }, [postToParent])

  // Pending context request resolvers keyed by nonce. The getContext callback
  // posts CONTEXT_REQUEST to the parent and resolves within ≤50ms timeout.
  const pendingContextResolversRef = useRef(new Map())
  let _contextNonce = 0

  // getContext: called by ChatView before submitting a message. Posts a
  // CONTEXT_REQUEST to the parent, waits ≤50ms for CONTEXT_RESPONSE, then
  // resolves (with null on timeout). The nonce correlates request ↔ response.
  const getContext = useCallback(() => {
    const target = parentRef.current
    if (!target || target === window) return Promise.resolve(null)
    const nonce = `ctx-${++_contextNonce}-${Date.now()}`
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        pendingContextResolversRef.current.delete(nonce)
        resolve(null)
      }, 50)
      pendingContextResolversRef.current.set(nonce, (ctx) => {
        clearTimeout(timer)
        resolve(ctx)
      })
      target.postMessage(
        { type: CONTEXT_REQUEST, instanceId: instanceIdRef.current, nonce },
        window.location.origin,
      )
    })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function postToParent(type, extra) {
    const target = parentRef.current
    if (!target || target === window) return // opened standalone, no parent
    target.postMessage(
      { type, instanceId: instanceIdRef.current, chatId: chatIdRef.current, ...extra },
      window.location.origin,
    )
  }

  useEffect(() => {
    // index.html paints a full-screen #splash over the SPA until the
    // normal AppRoot flow removes it. The embed branch bypasses AppRoot,
    // so remove it here or it covers the chat forever.
    try {
      const splash = document.getElementById('splash')
      if (splash) splash.remove()
    } catch {}
    function onMessage(event) {
      // Two inbound types: INIT (skip instanceId check — we're learning it)
      // and CONTEXT_RESPONSE (carries nonce + context for a pending request).
      // Source + origin are enforced for both.
      if (!isEmbedMessage(event, {
        origin: window.location.origin,
        expectedSource: parentRef.current,
      })) return
      const msg = event.data
      if (msg.type === CONTEXT_RESPONSE) {
        // Dispatch to any pending context resolver keyed by nonce.
        const resolver = pendingContextResolversRef.current.get(msg.nonce)
        if (resolver) {
          pendingContextResolversRef.current.delete(msg.nonce)
          resolver(msg.context || null)
        }
        return
      }
      if (msg.type !== INIT) return
      if (typeof msg.instanceId === 'string') instanceIdRef.current = msg.instanceId
      // The helper may pass an authoritative chatId in INIT (it
      // lazy-created one after opening us without a query param). Adopt
      // it when we don't already have one so ChatView mounts the real
      // chat instead of the no-chat notice.
      if (msg.chatId && !chatIdRef.current) setChatId(String(msg.chatId))
      if (typeof msg.picker === 'boolean') setPicker(msg.picker)
      // Extract quickActions from INIT payload (max 4, filtered in the runtime).
      if (Array.isArray(msg.quickActions) && msg.quickActions.length > 0) {
        setQuickActions(msg.quickActions)
      }
      // Re-announce, now correlated, so the parent learns the resolved
      // chatId under the right instanceId.
      postToParent(READY)
    }
    window.addEventListener('message', onMessage)
    // Announce mount immediately. The parent's INIT may have been posted
    // before this listener attached (iframe load race), so send an
    // uncorrelated READY now too; the parent keys off origin + source and
    // picks up the instanceId from its own INIT round-trip. Idempotent.
    postToParent(READY)
    return () => window.removeEventListener('message', onMessage)
    // Mount-only: refs hold the mutable bits and the listener reads the
    // latest chatId via chatIdRef. Re-subscribing on chatId change would
    // drop in-flight INIT handling.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (!chatId) {
    return (
      <div className="chat-embed chat-embed--empty">
        <p className="chat-embed__notice">
          Starting conversation…
        </p>
      </div>
    )
  }

  return (
    <ErrorBoundary
      label="chat-embed"
      variant="fullscreen"
      onReset={() => {
        // A render crash in ChatView shouldn't strand the embed — tell
        // the parent, then let the boundary remount the subtree (which
        // re-runs ChatView's durable reload).
        postToParent(ERROR, { error: 'render-crash' })
      }}
    >
      <div className="chat-embed">
        <ChatView
          key={chatId}
          chatId={chatId}
          embedded
          showPicker={picker}
          quickActions={quickActions}
          getContext={getContext}
          onMessageStart={() => {
            armTurnDone()
            postToParent(MESSAGE_SENT)
          }}
          onStreamEnd={notifyTurnDone}
          onExternalRunEvent={handleExternalRunEvent}
          // System events (theme_updated, app_created, …) are Shell-level
          // concerns. The embed is a chat renderer, so we drop them — but
          // pass the callback so ChatView never calls an undefined.
          onSystemEvent={() => {}}
        />
      </div>
    </ErrorBoundary>
  )
}
