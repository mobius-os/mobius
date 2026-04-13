import { useState, useRef, useEffect, useLayoutEffect, useCallback } from 'react'
import { apiFetch, getToken, BASE } from '../../api/client.js'
import { ProgressiveMarkdown } from './markdown/BlockRenderer.jsx'
import useStreamConnection from './useStreamConnection.js'
import useVoiceInput from './useVoiceInput.js'
import useFileUpload from './useFileUpload.js'
import ConnectionStatus from './ConnectionStatus.jsx'
import ToolBlock from './ToolBlock.jsx'
import MsgContent from './MsgContent.jsx'
import './ChatView.css'


// Cache touch-primary detection. Updated dynamically if input devices change.
const _touchMql = typeof matchMedia === 'function'
  ? matchMedia('(hover: none) and (pointer: coarse)')
  : null
let _isTouchPrimary = _touchMql?.matches ?? false
_touchMql?.addEventListener('change', (e) => { _isTouchPrimary = e.matches })

// Survive remounts: scroll and spacer state persisted in sessionStorage.
const _scrollPositions = (() => {
  try { return JSON.parse(sessionStorage.getItem('chat-scroll') || '{}') }
  catch { return {} }
})()
const _spacerHeights = (() => {
  try { return JSON.parse(sessionStorage.getItem('chat-spacer') || '{}') }
  catch { return {} }
})()


export default function ChatView({ chatId, onStreamEnd, onFirstMessage, onSystemEvent, builtApp, onOpenApp, onMessageStart }) {
  const [messages, setMessages] = useState([])
  const [totalMessages, setTotalMessages] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(true)
  const [sending, setSending] = useState(false)
  const [input, setInput] = useState(() => {
    try {
      const pending = sessionStorage.getItem('pending-draft')
      if (pending) { sessionStorage.removeItem('pending-draft'); return pending }
      return sessionStorage.getItem(`draft:${chatId}`) || ''
    } catch { return '' }
  })

  // DOM refs
  const scrollRef = useRef(null)
  const inputRef = useRef(null)
  const spacerRef = useRef(null)
  const fileInputRef = useRef(null)
  const lastUserMsgRef = useRef(null)

  // Lifecycle guards
  const chatIdStaleRef = useRef(false)
  const hadMessagesRef = useRef(false)
  const promotedRef = useRef(false)
  const needsScrollRef = useRef(false)

  // Spacer state — see CLAUDE.md "Chat UX — non-negotiable constraints".
  // spacerActive: keeps min-height: 0 on the list while the spacer is active,
  //   preventing min-height: calc(100% + 1px) from inflating offsetHeight.
  const [spacerActive, setSpacerActive] = useState(false)
  // Set in doSend, consumed by the spacer useLayoutEffect.
  const needsSpacerRef = useRef(false)
  // Persists scrollTarget across renders so promote can recalculate the spacer.
  const scrollTargetRef = useRef(null)
  // Full viewport height (keyboard closed). Set once on mount, used for spacer
  // sizing so keyboard open/close doesn't change scrollHeight.
  const fullViewHRef = useRef(0)
  // Tracks whether the user is near the bottom of the scroll area. Updated
  // by the passive scroll listener in the spacer effect. Used by
  // promoteStreamToMessages to decide whether to preserve scroll position
  // (user scrolled up to read) or allow re-anchoring (user was following).
  const nearBottomRef = useRef(false)
  // Ref mirror of `sending` for use in callbacks without adding it as a
  // dependency (avoids re-creating fetchMessages on every send).
  const sendingRef = useRef(false)
  sendingRef.current = sending

  // Re-fetch messages from the API. Called when the SSE stream reconnects
  // and gets a 204 (no active broadcast — the chat finished while the
  // user was offline or on poor connectivity). Replaces stale messages
  // with the current DB state.
  const fetchMessages = useCallback(async () => {
    if (sendingRef.current) return
    try {
      const res = await apiFetch(`/chats/${chatId}?limit=20`)
      const data = await res.json()
      if (chatIdStaleRef.current) return
      let msgs = data.messages || []
      for (const msg of msgs) {
        if (msg.blocks) {
          for (const blk of msg.blocks) {
            if (blk.type === 'tool' && blk.status === 'running') {
              blk.status = 'done'
            }
          }
        }
      }
      setMessages(msgs)
      setTotalMessages(data.total || 0)
      setOffset(data.offset || 0)
    } catch { /* network error — silent, user can retry */ }
  }, [chatId])

  const {
    streamItems,
    latestItemsRef,
    isStreaming,
    connectionError,
    sendMessage: streamSend,
    connectToStream,
    retry,
    disconnect,
  } = useStreamConnection(chatId, {
    onStreamEnd: () => {
      promoteStreamToMessages()
      setSending(false)
      onStreamEnd?.()
    },
    onSystemEvent,
    onNeedsRefresh: fetchMessages,
  })

  const { files: pendingFiles, addFiles, removeFile, clearFiles } = useFileUpload({ chatId })

  const { listening, listeningRef, toggleVoice } = useVoiceInput({
    onTranscript: (text) => setInput(text),
    inputRef,
  })

  // Snapshot stream into a permanent message. Idempotent — both handleStop
  // and onStreamEnd may call this.
  function promoteStreamToMessages() {
    if (promotedRef.current) return
    const items = latestItemsRef.current
    if (items.length === 0) return
    promotedRef.current = true
    // If the user scrolled up during streaming (not near the bottom),
    // null the scroll target so the spacer effect skips re-anchoring.
    // Without this, the content restructure triggers the ResizeObserver
    // which snaps them back to the bottom.  If the user IS near the
    // bottom (following the stream), preserve the scroll target so
    // auto-follow continues through the promote.
    if (!nearBottomRef.current) {
      scrollTargetRef.current = null
    }
    const blocks = items.map(item => {
      if (item.type === 'text') return { type: 'text', content: item.content }
      const status = item.status === 'running' ? 'done' : item.status
      return { type: 'tool', ...item, status }
    })
    const content = items
      .filter(i => i.type === 'text')
      .map(i => i.content)
      .join('')
    setMessages(prev => [...prev, { role: 'assistant', content, blocks }])
    setTotalMessages(t => t + 1)
  }

  // Persist draft so it survives leaving and re-entering the chat.
  useEffect(() => {
    try {
      if (input) sessionStorage.setItem(`draft:${chatId}`, input)
      else sessionStorage.removeItem(`draft:${chatId}`)
    } catch { /* quota exceeded or private browsing */ }
  }, [input, chatId])

  // Auto-size textarea when a draft is restored.
  useEffect(() => {
    const el = inputRef.current
    if (el && input) {
      el.style.height = 'auto'
      el.style.height = Math.min(el.scrollHeight, 160) + 'px'
    }
  }, [chatId])

  // Fetch messages and connect to an in-progress stream if the agent is running.
  useEffect(() => {
    let cancelled = false
    chatIdStaleRef.current = false

    apiFetch(`/chats/${chatId}?limit=20`)
      .then(r => r.json())
      .then(data => {
        if (cancelled) return
        let msgs = data.messages || []

        // Drop partial assistant message when the agent is still running —
        // the SSE catch-up burst will replay those events.
        const stripped = data.running && msgs.length > 0
          && msgs[msgs.length - 1].role === 'assistant'
        if (stripped) {
          msgs = msgs.slice(0, -1)
        }

        // Normalize stale "running" tool blocks from interrupted sessions.
        for (const msg of msgs) {
          if (msg.blocks) {
            for (const blk of msg.blocks) {
              if (blk.type === 'tool' && blk.status === 'running') {
                blk.status = 'done'
              }
            }
          }
        }

        setMessages(msgs)
        setTotalMessages((data.total || 0) - (stripped ? 1 : 0))
        setOffset(data.offset || 0)
        hadMessagesRef.current = msgs.length > 0
        setLoading(false)

        needsScrollRef.current = true

        if (data.running) {
          setSending(true)
          connectToStream(false)
        }
      })
      .catch(() => setLoading(false))

    return () => {
      try {
        sessionStorage.setItem('chat-scroll', JSON.stringify(_scrollPositions))
        sessionStorage.setItem('chat-spacer', JSON.stringify(_spacerHeights))
      } catch {}
      cancelled = true
      chatIdStaleRef.current = true
      scrollTargetRef.current = null
      loadingOlder.current = false
      disconnect()
    }
  }, [chatId])

  // Restore scroll position before paint, then re-apply after 300ms to
  // correct drift from lazy renderers (KaTeX, highlight.js).
  useLayoutEffect(() => {
    // Capture full viewport height as soon as the scroll element mounts.
    // Must happen outside the needsScrollRef guard — the scroll element may
    // not exist on the initial render (empty state), so the first render
    // where it appears may not have needsScrollRef set.
    const el = scrollRef.current
    if (el && !fullViewHRef.current) fullViewHRef.current = el.clientHeight

    if (!needsScrollRef.current) return
    needsScrollRef.current = false
    if (!el) return
    // Spacer height must be restored first — it affects scrollHeight.
    const savedSpacer = _spacerHeights[chatId]
    const sp = el.querySelector('.spacer-dynamic')
    if (savedSpacer && sp) {
      sp.style.height = savedSpacer
      setSpacerActive(true)
    }
    const saved = _scrollPositions[chatId]
    function applyScroll() {
      if (saved != null) {
        el.scrollTop = el.scrollHeight - saved
      } else {
        el.scrollTop = el.scrollHeight
      }
    }
    applyScroll()
    // Restore scrollTarget so the spacer effect sets up a ResizeObserver.
    // Without this, returning to a streaming chat leaves the spacer frozen
    // because scrollTargetRef was nulled on cleanup.  Use the last user
    // message's offsetTop (same as the send path) so the spacer formula
    // produces the correct height for keeping that message at the top.
    if (savedSpacer && parseInt(savedSpacer) > 0) {
      const userMsgs = el.querySelectorAll('.chat__msg--user')
      const lastUserEl = userMsgs[userMsgs.length - 1]
      scrollTargetRef.current = lastUserEl
        ? Math.max(0, lastUserEl.offsetTop - 4)
        : el.scrollTop
    }
    // Re-apply after lazy renderers settle.
    const tid = setTimeout(applyScroll, 300)
    return () => clearTimeout(tid)
  }, [messages])

  // ── Spacer: reserves space below the user's message ──────────────
  //
  // Formula: spacer = max(0, fullViewH + scrollTarget − listH)
  //
  // Three triggers (all via [messages] dependency):
  //   send    — set spacer, scroll to user message, start ResizeObserver
  //   promote — recalculate spacer only (don't touch scroll)
  //   mount   — skip (scroll-restore handles it)
  //
  // The spacer uses fullViewH (keyboard-closed viewport, captured on mount)
  // so that keyboard open/close doesn't change scrollHeight. Without this,
  // the browser resets scrollTop when the viewport resizes.
  //
  // The ResizeObserver only sets scrollTop in two cases:
  //   1. Content shrank (thinking dots removed) → browser clamped scrollTop
  //      in the async gap before the observer fired → correct it
  //   2. Content filled the viewport (spacer hit 0) → auto-follow if near bottom
  //
  // overflow-anchor: none is set on .chat__scroll in CSS to prevent Chrome's
  // scroll anchoring from fighting the spacer.
  //
  useLayoutEffect(() => {
    const isSend = needsSpacerRef.current
    needsSpacerRef.current = false

    // Mount/history — scroll-restore handles positioning.
    if (!isSend && scrollTargetRef.current == null) return

    const scrollEl = scrollRef.current
    const spacerEl = spacerRef.current
    if (!scrollEl || !spacerEl) return

    // Send — compute scroll target from the user message's position.
    if (isSend) {
      const userMsgEl = lastUserMsgRef.current
      if (!userMsgEl) return
      scrollTargetRef.current = Math.max(0, userMsgEl.offsetTop - 4)
    }

    // Recalculate spacer height (both send and promote paths).
    // Must happen before setting scrollTop — otherwise the browser clamps
    // scrollTop to a value that's too low.
    const st = scrollTargetRef.current
    const listEl = scrollEl.querySelector('.chat__list')
    if (!listEl) return
    const viewH = fullViewHRef.current || scrollEl.clientHeight
    const listH = listEl.offsetHeight
    const spacerH = Math.max(0, viewH + st - listH)
    spacerEl.style.height = `${spacerH}px`

    if (isSend) scrollEl.scrollTop = st

    // ResizeObserver: keep the spacer in sync as content streams in,
    // tool blocks expand, or lazy renderers (highlight.js, KaTeX) resize.
    // Set up on every path (send, promote, reconnect) — not just send —
    // so the spacer stays correct after switching chats and returning.
    let prevH = spacerH
    // Track whether the user is near the bottom so auto-follow works
    // during streaming.  Updated on EVERY scroll event (including
    // programmatic snaps) so the ResizeObserver always has a fresh
    // reading — no stale-by-one-tick problem.
    // Start with auto-follow OFF. The send path scrolls to show the
    // user's message at the top of the viewport; the agent's response
    // appears just below it in the visible area. Auto-follow only
    // engages when the user actively scrolls to the bottom of content
    // that OVERFLOWS the viewport. Without the overflow check, the
    // first message (where content fits on screen → gap≈0) would
    // falsely engage auto-follow, yanking the user as the response
    // grows past the fold.
    let nearBottom = false
    const onScroll = () => {
      const g = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight
      const overflows = scrollEl.scrollHeight > scrollEl.clientHeight + 50
      nearBottom = overflows && g < 50
      nearBottomRef.current = nearBottom
    }
    scrollEl.addEventListener('scroll', onScroll, { passive: true })

    const ro = new ResizeObserver(() => {
      if (scrollTargetRef.current == null) return
      const target = scrollTargetRef.current
      const lH = listEl.offsetHeight
      const h = Math.max(0, viewH + target - lH)
      spacerEl.style.height = `${h}px`

      // Content shrank (spacer grew) — browser may have clamped scrollTop
      // in the gap before this callback. Correct it.
      if (h > prevH && scrollEl.scrollTop < target) {
        scrollEl.scrollTop = target
      }
      prevH = h

      // Auto-follow: if the user is near the bottom (updated in real
      // time by the scroll listener above), snap to bottom.  The scroll
      // listener fires on the programmatic snap too, keeping nearBottom
      // true for the next resize.  When the user scrolls up past 50px,
      // the listener sets nearBottom=false and auto-follow stops.
      const gap = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight
      if (nearBottom && gap > 1) {
        scrollEl.scrollTop = scrollEl.scrollHeight
      }
    })
    ro.observe(listEl)
    return () => {
      ro.disconnect()
      scrollEl.removeEventListener('scroll', onScroll)
    }
  }, [messages])


  // Paginate older messages when scrolled to top or button clicked.
  const loadingOlder = useRef(false)
  function loadOlderMessages() {
    const el = scrollRef.current
    if (!el || loadingOlder.current || loading || offset <= 0) return
    loadingOlder.current = true
    const prevHeight = el.scrollHeight
    apiFetch(`/chats/${chatId}?limit=20&before=${offset}`)
      .then(r => r.json())
      .then(data => {
        if (chatIdStaleRef.current) return
        const older = data.messages || []
        for (const msg of older) {
          if (msg.blocks) {
            for (const blk of msg.blocks) {
              if (blk.type === 'tool' && blk.status === 'running') {
                blk.status = 'done'
              }
            }
          }
        }
        setMessages(prev => [...older, ...prev])
        setOffset(data.offset || 0)
        requestAnimationFrame(() => {
          const scrollEl = scrollRef.current
          if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight - prevHeight
          loadingOlder.current = false
        })
      })
      .catch(() => { loadingOlder.current = false })
  }

  function handleScroll() {
    const el = scrollRef.current
    if (!el || loadingOlder.current || loading) return
    _scrollPositions[chatId] = el.scrollHeight - el.scrollTop
    const sp = el.querySelector('.spacer-dynamic')
    if (sp) _spacerHeights[chatId] = sp.style.height
    if (el.scrollTop < 5 && offset > 0) {
      loadOlderMessages()
    }
  }


  function handleFileSelect(e) {
    const fileList = Array.from(e.target.files || [])
    if (!fileList.length) return
    e.target.value = ''
    addFiles(fileList)
  }

  const doSend = useCallback(async (text) => {
    if (!text.trim() || sending) return
    onMessageStart?.()
    promotedRef.current = false

    const attachments = pendingFiles
      .filter(f => f.status === 'done')
      .map(f => ({ name: f.name, size: f.size, mime_type: f.mime_type }))

    const userMsg = { role: 'user', content: text, ts: Date.now() }
    if (attachments.length > 0) userMsg.attachments = attachments
    setMessages(prev => [...prev, userMsg])
    setTotalMessages(t => t + 1)
    setInput('')
    clearFiles()
    if (inputRef.current) inputRef.current.style.height = 'auto'
    setSending(true)
    setSpacerActive(true)
    if (spacerRef.current) spacerRef.current.style.height = '0px'
    needsSpacerRef.current = true

    try {
      await streamSend(text, attachments.length > 0 ? attachments : undefined)
      if (!hadMessagesRef.current) {
        hadMessagesRef.current = true
        onFirstMessage?.()
      }
    } catch (err) {
      setSending(false)
      setMessages(prev => [
        ...prev,
        { role: 'assistant', content: `Error: ${err.message}`, blocks: [] },
      ])
    }
  }, [sending, streamSend, pendingFiles])

  function handleSubmit(e) {
    e.preventDefault()
    doSend(input.trim())
  }

  async function handleStop() {
    try {
      await fetch(`${BASE}/api/chat/stop`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${getToken()}`,
        },
        body: JSON.stringify({ chat_id: chatId }),
      })
    } catch { /* network error during stop is non-critical */ }
    promoteStreamToMessages()
    disconnect()
    setSending(false)
    onStreamEnd?.()
  }

  const hasMore = offset > 0
  const showEmpty = messages.length === 0 && !isStreaming && !loading && !sending
  const lastUserIdx = messages.reduce((acc, m, i) => m.role === 'user' ? i : acc, -1)

  return (
    <div className={`chat${showEmpty ? ' chat--empty' : ''}`}>
      {showEmpty && (
        <div className="chat__empty-wrap">
          <div className="chat__empty">
            <img className="chat__empty-glyph" src={`${BASE}/moebius.png`} alt="" width="120" height="120" />
            <p className="chat__empty-title">What's on your mind?</p>
            <p className="chat__empty-sub">Try: "What can you do?" or "Build me a weather app"<br />The agent learns from every session and gets better over time.</p>
          </div>
        </div>
      )}
      {!showEmpty && (
      <div className="chat__scroll" ref={scrollRef} onScroll={handleScroll}>
        <ul className="chat__list" style={spacerActive ? { minHeight: 0 } : undefined}>
          {hasMore && (
            <li className="chat__older">
              <button onClick={loadOlderMessages}>Load earlier messages</button>
            </li>
          )}

          {messages.map((msg, i) => (
            <li
              key={msg.id || msg.ts || `${msg.role}-${i}`}
              className={`chat__msg chat__msg--${msg.role}`}
              ref={i === lastUserIdx ? lastUserMsgRef : undefined}
              onClick={msg.ts && msg.role === 'user'
                ? (e) => { e.currentTarget.querySelector('.chat__ts')?.classList.toggle('chat__ts--visible') }
                : undefined}
            >
              <MsgContent msg={msg} chatId={chatId} />
              {msg.ts && msg.role === 'user' && (
                <time className="chat__ts">
                  {new Date(msg.ts).toLocaleString([], {
                    month: 'short', day: 'numeric',
                    hour: '2-digit', minute: '2-digit',
                  })}
                </time>
              )}
            </li>
          ))}

          {sending && streamItems.length > 0 && (
            <li className="chat__msg chat__msg--assistant">
              {streamItems.map((item, i) => {
                if (item.type === 'tool') {
                  return (
                    <div key={`s-${i}`} className="chat__tools">
                      <ToolBlock t={item} />
                    </div>
                  )
                }
                if (item.type === 'text') {
                  const isLast = i === streamItems.length - 1
                  return (
                    <div key={`s-${i}`} className="chat__text chat__text--assistant">
                      <ProgressiveMarkdown text={item.content} />
                      {isLast && <span className="chat__cursor" />}
                    </div>
                  )
                }
                return null
              })}
            </li>
          )}

          {sending && streamItems.length === 0 && !loading && (
            <li className="chat__msg chat__msg--assistant">
              <div className="chat__thinking"><span /><span /><span /></div>
            </li>
          )}
        </ul>

        <div className="spacer-dynamic" ref={spacerRef} aria-hidden="true" />
      </div>
      )}

      {builtApp && !sending && (
        <div className="chat__open-app">
          <button
            className="chat__open-app-btn"
            onClick={() => onOpenApp?.(builtApp.id)}
          >
            Open {builtApp.name || 'app'} →
          </button>
        </div>
      )}

      <ConnectionStatus error={connectionError} onRetry={retry} />

      <div className="chat__foot">
        <form className="chat__form" onSubmit={handleSubmit}>
          {pendingFiles.length > 0 && (
            <div className="chat__chips">
              {pendingFiles.map(chip => (
                <div
                  key={chip.id}
                  className={`chat__chip${chip.status === 'error' ? ' chat__chip--error' : ''}${chip.objectUrl ? ' chat__chip--image' : ''}`}
                  title={chip.status === 'error' ? chip.error : chip.name}
                >
                  {chip.objectUrl && (
                    <img className="chat__chip-thumb" src={chip.objectUrl} alt="" />
                  )}
                  <span className="chat__chip-name">{chip.name}</span>
                  <span className="chat__chip-status">
                    {chip.status === 'uploading' ? 'uploading…' : chip.status === 'error' ? 'error' : `${Math.round(chip.size / 1024)}KB`}
                  </span>
                  <button
                    type="button"
                    className="chat__chip-remove"
                    onClick={() => removeFile(chip.id)}
                    aria-label={`Remove ${chip.name}`}
                  >×</button>
                </div>
              ))}
            </div>
          )}
          <div className="chat__input-row">
            <input
              type="file"
              multiple
              ref={fileInputRef}
              onChange={handleFileSelect}
              style={{ display: 'none' }}
            />
            <button
              type="button"
              className="chat__attach"
              onClick={() => fileInputRef.current?.click()}
              aria-label="Attach files"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
              </svg>
            </button>
            <textarea
              ref={inputRef}
              className="chat__input"
              value={input}
              onChange={(e) => {
                if (listeningRef.current) return
                setInput(e.target.value)
                e.target.style.height = 'auto'
                e.target.style.height = Math.min(e.target.scrollHeight, 160) + 'px'
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && !_isTouchPrimary) {
                  e.preventDefault(); handleSubmit(e)
                }
              }}
              placeholder="Message the agent..."
              rows={1}
            />
            {sending ? (
              <button className="chat__stop" type="button" onClick={handleStop} aria-label="Stop">
                <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor">
                  <rect width="12" height="12" rx="2" />
                </svg>
              </button>
            ) : (input.trim() && !listening) ? (
              <button
                className="chat__send"
                type="button"
                onClick={handleSubmit}
                aria-label="Send"
                disabled={pendingFiles.some(c => c.status === 'uploading')}
              >
                <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
                  <path d="M6.5 11V2M2 6.5l4.5-4.5 4.5 4.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              </button>
            ) : (
              <button
                className={`chat__mic ${listening ? 'chat__mic--active' : ''}`}
                type="button"
                onTouchEnd={(e) => { e.preventDefault(); toggleVoice() }}
                onClick={toggleVoice}
                aria-label={listening ? 'Stop recording' : 'Voice input'}
              >
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                  <rect x="4.5" y="1" width="5" height="8" rx="2.5" stroke="currentColor" strokeWidth="1.3"/>
                  <path d="M3 7a4 4 0 008 0" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
                  <path d="M7 11v2" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
                </svg>
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  )
}
