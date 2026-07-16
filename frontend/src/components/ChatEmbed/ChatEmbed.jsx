import { useCallback, useEffect, useRef, useState } from 'react'
import ChatView from '../ChatView/ChatView.jsx'
import ErrorBoundary from '../ErrorBoundary/ErrorBoundary.jsx'
import {
  BASE,
  clearEphemeralAuthSession,
  setEphemeralAuthSession,
} from '../../api/client.js'
import { applyThemeToDom } from '../../lib/themeService.js'
import {
  INIT, READY, MESSAGE_SENT, TURN_DONE, ERROR, AUTH_EXPIRING,
  CONTEXT_REQUEST, CONTEXT_RESPONSE,
  isEmbedMessage, retainEmbedSessionAfterExchangeFailure,
} from '../../lib/chatEmbed.js'
import {
  EMPTY_TURN_DONE_GATE,
  advanceTurnDoneGate,
} from '../../lib/chatRunSignal.js'
import './ChatEmbed.css'

// This document is intentionally inert after navigation. The opaque outer app
// sandbox propagates inward, so it cannot read owner localStorage and does not
// mount ChatView until a one-use server capability has been exchanged. Source,
// null-origin and correlation checks below are browser routing guards only;
// authorization is the server-verified capability/session.
export default function ChatEmbed() {
  const [authorized, setAuthorized] = useState(false)
  const [chatId, setChatId] = useState(null)
  const [picker, setPicker] = useState(true)
  const [quickActions, setQuickActions] = useState(null)
  const authorizedRef = useRef(false)
  const parentRef = useRef(typeof window !== 'undefined' ? window.parent : null)
  const instanceIdRef = useRef(null)
  const chatIdRef = useRef(null)
  const exchangeRef = useRef({ capability: null, promise: null })
  const latestAuthorizationIdRef = useRef(null)
  const refreshTimerRef = useRef(null)
  const contextNonceRef = useRef(0)
  const pendingContextResolversRef = useRef(new Map())
  const turnDoneGateRef = useRef(EMPTY_TURN_DONE_GATE)

  function postToParent(type, extra) {
    const target = parentRef.current
    if (!target || target === window || !instanceIdRef.current) return
    // Opaque targets cannot be named as a postMessage targetOrigin. The exact
    // WindowProxy + instance id constrain in-browser routing; neither is auth.
    target.postMessage({
      type,
      instanceId: instanceIdRef.current,
      chatId: chatIdRef.current,
      ...extra,
    }, '*')
  }

  const armTurnDone = useCallback(() => {
    turnDoneGateRef.current = advanceTurnDoneGate(
      turnDoneGateRef.current, 'message_started',
    ).gate
  }, [])

  const notifyTurnDone = useCallback(({ continues = false } = {}) => {
    const result = advanceTurnDoneGate(
      turnDoneGateRef.current,
      continues ? 'stream_continues' : 'stream_finished',
    )
    turnDoneGateRef.current = result.gate
    if (result.emit) postToParent(TURN_DONE)
  }, [])

  const handleExternalRunEvent = useCallback((eventType) => {
    const result = advanceTurnDoneGate(turnDoneGateRef.current, eventType)
    turnDoneGateRef.current = result.gate
    if (result.emit) postToParent(TURN_DONE)
  }, [])

  const getContext = useCallback(() => {
    const target = parentRef.current
    if (!target || target === window || !instanceIdRef.current) {
      return Promise.resolve(null)
    }
    const nonce = `ctx-${++contextNonceRef.current}-${Date.now()}`
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        pendingContextResolversRef.current.delete(nonce)
        resolve(null)
      }, 50)
      pendingContextResolversRef.current.set(nonce, (context) => {
        clearTimeout(timer)
        resolve(context)
      })
      target.postMessage({
        type: CONTEXT_REQUEST,
        instanceId: instanceIdRef.current,
        nonce,
      }, '*')
    })
  }, [])

  useEffect(() => {
    try { document.getElementById('splash')?.remove() } catch {}

    async function establish(msg) {
      const capability = msg.bootstrapCapability
      const authorizationId = msg.authorizationId
      if (
        typeof capability !== 'string'
        || capability.length < 32
        || typeof authorizationId !== 'string'
        || authorizationId.length < 8
        || typeof msg.chatId !== 'string'
        || typeof msg.instanceId !== 'string'
      ) return
      if (exchangeRef.current.capability === capability) {
        await exchangeRef.current.promise
        return
      }
      if (
        authorizedRef.current
        && (msg.chatId !== chatIdRef.current || msg.instanceId !== instanceIdRef.current)
      ) return

      latestAuthorizationIdRef.current = authorizationId
      instanceIdRef.current = msg.instanceId
      const exchange = (async () => {
        const response = await fetch(`${BASE}/api/app-chat-embeds/session`, {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${capability}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ instance_id: msg.instanceId }),
        })
        if (!response.ok) throw new Error(`authorization failed (${response.status})`)
        const session = await response.json()
        if (
          session.chat_id !== msg.chatId
          || session.instance_id !== msg.instanceId
          || session.role !== 'participant'
        ) throw new Error('authorization response mismatch')
        // A parent acknowledgement timeout may already have started a newer
        // one-use exchange. Never let a late response overwrite that newer
        // in-memory handoff; the server independently enforces grant order.
        if (latestAuthorizationIdRef.current !== authorizationId) return

        // The old same-origin renderer loaded /api/theme with the app token.
        // The stronger contract keeps generic APIs closed: the verified
        // exchange returns the already-app-visible theme and we apply it only
        // after the exact chat/instance/role checks above have succeeded.
        if (session.theme?.css) {
          applyThemeToDom(
            session.theme.css,
            session.theme.bg,
            session.theme.mode,
            { animate: false },
          )
        }

        setEphemeralAuthSession(session.token, msg.instanceId)
        chatIdRef.current = session.chat_id
        setChatId(session.chat_id)
        setPicker(typeof msg.picker === 'boolean' ? msg.picker : true)
        setQuickActions(Array.isArray(msg.quickActions) ? msg.quickActions : null)
        setAuthorized(true)
        authorizedRef.current = true

        if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current)
        const expiry = Date.parse(session.expires_at)
        const delay = Number.isFinite(expiry)
          ? Math.max(1000, expiry - Date.now() - 2 * 60 * 1000)
          : 10 * 60 * 1000
        refreshTimerRef.current = setTimeout(() => postToParent(AUTH_EXPIRING), delay)
        postToParent(READY, { authorizationId })
      })().catch((error) => {
        if (latestAuthorizationIdRef.current !== authorizationId) return
        const retainExisting = retainEmbedSessionAfterExchangeFailure(
          authorizedRef.current,
        )
        if (!retainExisting) {
          clearEphemeralAuthSession()
          authorizedRef.current = false
          setAuthorized(false)
        }
        postToParent(ERROR, {
          phase: 'authorization',
          authorizationId,
          refresh: retainExisting,
          error: error.message || 'authorization failed',
        })
      })
      exchangeRef.current = { capability, promise: exchange }
      await exchange
    }

    function onMessage(event) {
      if (!isEmbedMessage(event, {
        origins: ['null', window.location.origin],
        expectedSource: parentRef.current,
      })) return
      const msg = event.data
      if (msg.type === INIT) {
        establish(msg).catch(() => {})
        return
      }
      if (
        msg.type !== CONTEXT_RESPONSE
        || msg.instanceId !== instanceIdRef.current
      ) return
      const resolver = pendingContextResolversRef.current.get(msg.nonce)
      if (resolver) {
        pendingContextResolversRef.current.delete(msg.nonce)
        resolver(msg.context || null)
      }
    }

    function onAuthExpired() {
      postToParent(AUTH_EXPIRING)
    }

    window.addEventListener('message', onMessage)
    window.addEventListener('mobius:chat-embed-auth-expired', onAuthExpired)
    return () => {
      window.removeEventListener('message', onMessage)
      window.removeEventListener('mobius:chat-embed-auth-expired', onAuthExpired)
      if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current)
      for (const resolve of pendingContextResolversRef.current.values()) resolve(null)
      pendingContextResolversRef.current.clear()
      clearEphemeralAuthSession()
      authorizedRef.current = false
      latestAuthorizationIdRef.current = null
    }
  }, []) // authorization state is held in refs after the one inert mount

  // Do not render a chat id, loading text, cached data or any active controls
  // before the server has established the exact scoped principal.
  if (!authorized || !chatId) return <div className="chat-embed" aria-hidden="true" />

  return (
    <ErrorBoundary
      label="chat-embed"
      variant="fullscreen"
      onReset={() => postToParent(ERROR, { error: 'render-crash' })}
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
          onSystemEvent={() => {}}
        />
      </div>
    </ErrorBoundary>
  )
}
