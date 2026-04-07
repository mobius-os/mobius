import { useState, useRef, useEffect, useLayoutEffect, useCallback } from 'react'
import { apiFetch, getToken } from '../../api/client.js'
import { ProgressiveMarkdown } from './markdown/BlockRenderer.jsx'
import useStreamConnection from './useStreamConnection.js'
import useVoiceInput from './useVoiceInput.js'
import useFileUpload from './useFileUpload.js'
import ConnectionStatus from './ConnectionStatus.jsx'
import ToolBlock from './ToolBlock.jsx'
import MsgContent from './MsgContent.jsx'
import './ChatView.css'


// Module-level maps so scroll positions and spacer heights survive remounts.
const _scrollPositions = (() => {
  try { return JSON.parse(sessionStorage.getItem('chat-scroll') || '{}') }
  catch { return {} }
})()
const _spacerHeights = (() => {
  try { return JSON.parse(sessionStorage.getItem('chat-spacer') || '{}') }
  catch { return {} }
})()

export default function ChatView({ chatId, onStreamEnd, onFirstMessage, onSystemEvent, pendingReport, onReportConsumed, builtApp, onOpenApp, onMessageStart }) {
  const [messages, setMessages] = useState([])
  const [totalMessages, setTotalMessages] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(true)
  const [sending, setSending] = useState(false)
  const [input, setInput] = useState(() => {
    try { return sessionStorage.getItem(`draft:${chatId}`) || '' } catch { return '' }
  })

  const scrollRef = useRef(null)
  const inputRef = useRef(null)
  const spacerRef = useRef(null)
  const fileInputRef = useRef(null)
  const lastUserMsgRef = useRef(null)
  const chatIdStaleRef = useRef(false)
  const hadMessagesRef = useRef(false)
  const promotedRef = useRef(false)
  const needsScrollRef = useRef(false)
  // Keeps min-height: 0 on the list while the spacer is active, preventing
  // min-height: calc(100% + 1px) from inflating scrollHeight.
  const [spacerActive, setSpacerActive] = useState(false)
  // Set in doSend, consumed by the spacer useLayoutEffect.
  const needsSpacerRef = useRef(false)
  // Persists scrollTarget across effect runs so the spacer can recalculate
  // after promote/stop without a full re-init.
  const scrollTargetRef = useRef(null)

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
  })

  const { files: pendingFiles, addFiles, removeFile, clearFiles } = useFileUpload({ chatId })

  const { listening, listeningRef, toggleVoice } = useVoiceInput({
    onTranscript: (text) => setInput(text),
    inputRef,
  })

  // Converts current streamItems to a message and appends to messages state.
  // Idempotent via promotedRef — handleStop and onStreamEnd can both call this.
  function promoteStreamToMessages() {
    if (promotedRef.current) return
    const items = latestItemsRef.current
    if (items.length === 0) return
    promotedRef.current = true
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

  // Persist draft input so it survives leaving and re-entering the chat.
  useEffect(() => {
    try {
      if (input) sessionStorage.setItem(`draft:${chatId}`, input)
      else sessionStorage.removeItem(`draft:${chatId}`)
    } catch { /* quota exceeded or private browsing */ }
  }, [input, chatId])

  // Auto-size textarea on mount when a draft is restored from session storage.
  useEffect(() => {
    const el = inputRef.current
    if (el && input) {
      el.style.height = 'auto'
      el.style.height = Math.min(el.scrollHeight, 160) + 'px'
    }
  }, [chatId])

  // Fetch messages on mount.
  useEffect(() => {
    let cancelled = false
    chatIdStaleRef.current = false

    apiFetch(`/chats/${chatId}?limit=20`)
      .then(r => r.json())
      .then(data => {
        if (cancelled) return
        let msgs = data.messages || []

        // If the agent is still running, the DB has a partial assistant
        // message saved incrementally during streaming.  The SSE catch-up
        // burst will replay those same events, so drop the partial message
        // to avoid rendering the initial response twice.
        const stripped = data.running && msgs.length > 0
          && msgs[msgs.length - 1].role === 'assistant'
        if (stripped) {
          msgs = msgs.slice(0, -1)
        }

        // Fix stale "running" tool blocks in historical messages.
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

  // Restore scroll position after React commits loaded messages to the DOM.
  // Applies immediately (before paint) then again after lazy renderers
  // (KaTeX, highlight.js) reflow content — corrects any height drift
  // without hiding or flashing.
  useLayoutEffect(() => {
    if (!needsScrollRef.current) return
    needsScrollRef.current = false
    const el = scrollRef.current
    if (!el) return
    // Restore spacer height first — it affects scrollHeight.
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
    // Re-apply after lazy rendering settles (KaTeX, highlight.js).
    const tid = setTimeout(applyScroll, 300)
    return () => clearTimeout(tid)
  }, [messages])

  // ── Spacer: ensures at least a viewport of space below the user's message ──
  //
  // Formula: spacer = max(0, viewH + scrollTarget − listH)
  //
  // On send (needsSpacerRef=true): computes scrollTarget from the last user
  // message, scrolls to it, and sets up a ResizeObserver to keep the spacer
  // in sync as content streams in.
  //
  // On promote/stop (needsSpacerRef=false, scrollTargetRef set): recalculates
  // the spacer once without touching scroll — compensates for content height
  // changes when thinking dots are removed or stream items are promoted.
  //
  // On mount/history (scrollTargetRef=null): does nothing — scroll restoration
  // handles positioning, spacer height is restored from sessionStorage.
  useLayoutEffect(() => {
    const isSend = needsSpacerRef.current
    needsSpacerRef.current = false
    const scrollEl = scrollRef.current
    const userMsgEl = lastUserMsgRef.current
    const spacerEl = spacerRef.current
    if (!scrollEl || !spacerEl) return

    if (isSend) {
      if (!userMsgEl) return
      scrollTargetRef.current = Math.max(0, userMsgEl.offsetTop - 4)
      scrollEl.scrollTop = scrollTargetRef.current
    }

    if (scrollTargetRef.current == null) return

    // Recalculate spacer height.
    const scrollTarget = scrollTargetRef.current
    const listEl = scrollEl.querySelector('.chat__list')
    if (!listEl) return
    const viewH = scrollEl.clientHeight
    const listH = listEl.offsetHeight
    spacerEl.style.height = `${Math.max(0, viewH + scrollTarget - listH)}px`

    if (!isSend) return

    // Live send — observe content changes for streaming.
    function update() {
      const st = scrollTargetRef.current
      if (st == null) return
      const vH = scrollEl.clientHeight
      const lH = listEl.offsetHeight
      const h = Math.max(0, vH + st - lH)
      spacerEl.style.height = `${h}px`
      if (h > 0) {
        // Content shorter than viewport — hold at user message.
        scrollEl.scrollTop = st
      } else {
        // Content exceeds viewport — follow growth if near bottom.
        const gap = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight
        if (gap < 50) scrollEl.scrollTop = scrollEl.scrollHeight
      }
    }

    const ro = new ResizeObserver(update)
    ro.observe(listEl)
    window.addEventListener('resize', update)

    return () => {
      ro.disconnect()
      window.removeEventListener('resize', update)
    }
  }, [messages])


  // Load older messages — called from the scroll handler (near top) and the button.
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

  // Auto-submit error reports from mini-apps via the same sendMessage path.
  useEffect(() => {
    if (pendingReport && !sending) {
      const text = pendingReport
      onReportConsumed?.()
      doSend(text)
    }
  }, [pendingReport, sending, onReportConsumed, doSend])

  function handleSubmit(e) {
    e.preventDefault()
    doSend(input.trim())
  }

  async function handleStop() {
    try {
      await fetch('/api/chat/stop', {
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
            <img className="chat__empty-glyph" src="/moebius.png" alt="" width="120" height="120" />
            <p className="chat__empty-title">What's on your mind?</p>
            <p className="chat__empty-sub">Build apps, ask questions, tweak the interface.<br />The agent learns from every session and gets better over time.</p>
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
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit(e) }
              }}
              placeholder="Message the agent..."
              disabled={sending}
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
