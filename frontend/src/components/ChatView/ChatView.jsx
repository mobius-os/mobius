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


// Module-level map so scroll positions survive component remounts (key={chatId}).
const _scrollPositions = (() => {
  try { return JSON.parse(sessionStorage.getItem('chat-scroll') || '{}') }
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
  // True while the spacer is actively managing layout (from doSend until
  // the user scrolls).  Keeps min-height: 0 on the list even after
  // sending ends, preventing the min-height from inflating scrollHeight
  // and creating extra scroll space.
  const [spacerActive, setSpacerActive] = useState(false)
  // Flag for the useLayoutEffect that positions the spacer after a send.
  const needsSpacerRef = useRef(false)

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
      // Promote streamed items directly to the messages array.
      // No server fetch — the server saved incrementally during streaming,
      // and re-fetching causes a visible re-render jitter.
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
  // Uses a flag to ensure idempotency — handleStop and the SSE onStreamEnd
  // callback can both call this concurrently.  The flag is reset in doSend
  // when a new message starts.
  function promoteStreamToMessages() {
    if (promotedRef.current) return
    const items = latestItemsRef.current
    if (items.length === 0) return
    promotedRef.current = true
    const blocks = items.map(item => {
      if (item.type === 'text') return { type: 'text', content: item.content }
      // Normalize any still-running tools to done — the backend's final save
      // does the same, but the "done" SSE event arrives before that save runs.
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
        // to avoid rendering the initial response twice.  Adjust totalMessages
        // to match — promoteStreamToMessages will increment it back when the
        // stream completes.
        const stripped = data.running && msgs.length > 0
          && msgs[msgs.length - 1].role === 'assistant'
        if (stripped) {
          msgs = msgs.slice(0, -1)
        }

        // Fix stale "running" tool blocks in historical messages.
        // If the agent crashed or the CLI exited before emitting tool_end,
        // the DB keeps status:"running" forever.  Always normalize — when
        // the agent is still running, we strip the partial last message
        // above and reconnect to SSE, which sets live tools to "running".
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

        // Flag for useLayoutEffect to restore scroll after React commits the DOM.
        needsScrollRef.current = true

        // If agent is running, connect to the live SSE stream.
        if (data.running) {
          setSending(true)
          connectToStream(false)
        }
      })
      .catch(() => setLoading(false))

    return () => {
      // Persist scroll positions to sessionStorage so they survive page reloads.
      try { sessionStorage.setItem('chat-scroll', JSON.stringify(_scrollPositions)) } catch {}
      cancelled = true
      chatIdStaleRef.current = true
      loadingOlder.current = false
      disconnect()
    }
  }, [chatId])

  // Restore scroll position after React commits loaded messages to the DOM.
  // useLayoutEffect fires synchronously after DOM mutations, before paint —
  // no flash. The flag ensures this only runs on initial chat load, not
  // on every messages state change (which would cause repeated flashing).
  useLayoutEffect(() => {
    if (!needsScrollRef.current) return
    needsScrollRef.current = false
    const el = scrollRef.current
    if (!el) return
    const saved = _scrollPositions[chatId]
    if (saved != null) {
      el.scrollTop = el.scrollHeight - saved
    } else {
      el.scrollTop = el.scrollHeight
    }
  }, [messages])

  // ── Spacer: keeps scrollHeight constant so the message stays put ──
  //
  // Formula: spacer = max(0, viewH + scrollTarget − listH)
  //
  // This ensures maxScrollTop = scrollTarget at all times.  As content
  // grows (streaming), the spacer shrinks by exactly the same amount,
  // keeping scrollHeight = viewH + scrollTarget.  When content shrinks
  // (stop removes thinking dots), the spacer grows back.  The scroll
  // position never needs to change.
  //
  // Uses direct DOM manipulation (no React state) because the spacer
  // div has no style prop — React never touches it.  A ResizeObserver
  // on the list catches ALL content changes (streaming, promote, stop)
  // in one place.
  //
  // IMPORTANT: uses listEl.offsetHeight, NOT scrollEl.scrollHeight.
  // A scroll container's scrollHeight never goes below clientHeight,
  // so when content is short it equals the viewport height — useless.
  useLayoutEffect(() => {
    if (!needsSpacerRef.current) return
    needsSpacerRef.current = false
    const scrollEl = scrollRef.current
    const userMsgEl = lastUserMsgRef.current
    const spacerEl = spacerRef.current
    if (!scrollEl || !userMsgEl || !spacerEl) return
    const scrollTarget = Math.max(0, userMsgEl.offsetTop - 4)
    const listEl = scrollEl.querySelector('.chat__list')
    if (!listEl) return
    // Track the largest viewport seen (keyboard open vs closed).
    let maxViewH = scrollEl.clientHeight

    function update() {
      const viewH = scrollEl.clientHeight
      if (viewH > maxViewH) maxViewH = viewH
      const listH = listEl.offsetHeight
      spacerEl.style.height = `${Math.max(0, maxViewH + scrollTarget - listH)}px`
      // Auto-scroll: if near the bottom, follow content growth.
      // Uses scrollHeight (not scrollTarget) so that once content exceeds
      // the viewport and the spacer hits 0, we follow the new content
      // instead of yanking back to the message.  When the spacer is still
      // active, scrollHeight clamps to scrollTarget anyway.
      const gap = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight
      if (gap < 50) scrollEl.scrollTop = scrollEl.scrollHeight
    }

    update()
    scrollEl.scrollTop = scrollTarget

    // ResizeObserver on the list catches everything: streaming content
    // growth, thinking dots appearing/disappearing, promote, stop.
    const ro = new ResizeObserver(update)
    ro.observe(listEl)
    // Keyboard dismiss / rotation.
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
        // Older messages are always historical — fix stale running tools.
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
    // Continuously save scroll as distance-from-bottom.
    // Can't save in useEffect cleanup — DOM is already detached, scrollTop reads 0.
    _scrollPositions[chatId] = el.scrollHeight - el.scrollTop
    if (el.scrollTop < 5 && offset > 0) {
      loadOlderMessages()
    }
  }


  function handleFileSelect(e) {
    const fileList = Array.from(e.target.files || [])
    if (!fileList.length) return
    // Reset the input so the same file can be re-selected after removal.
    e.target.value = ''
    addFiles(fileList)
  }

  const doSend = useCallback(async (text) => {
    if (!text.trim() || sending) return
    onMessageStart?.()
    promotedRef.current = false

    // Build attachments from completed pending files.
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
      // Refresh chat list so this chat appears in the drawer immediately
      // on the first message, rather than waiting for stream end.
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
    // promoteStreamToMessages normalizes running tools to done internally,
    // so no need to mutate streamItems beforehand.
    promoteStreamToMessages()
    disconnect()
    setSending(false)
    onStreamEnd?.()
  }

  const hasMore = offset > 0
  const showEmpty = messages.length === 0 && !isStreaming && !loading && !sending
  // Index of the last user message — used to assign lastUserMsgRef to
  // exactly one element.  Assigning the same ref to multiple elements
  // is unreliable: React doesn't re-assign refs on existing elements.
  const lastUserIdx = messages.reduce((acc, m, i) => m.role === 'user' ? i : acc, -1)

  return (
    <div className={`chat${showEmpty ? ' chat--empty' : ''}`}>
      {/* Empty state is rendered outside the scroll area so it can be
          vertically centered without fighting scroll/spacer/min-height. */}
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

        {/* min-height is disabled while the spacer is managing layout.
            min-height: calc(100% + 1px) inflates scrollHeight and creates
            extra scroll space that conflicts with the spacer.  It's only
            needed for iOS elastic bounce in the normal scroll flow. */}
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

          {/* Streaming response — items rendered in arrival order. */}
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

        {/* DO NOT add a CSS transition to .spacer-dynamic — it breaks
            the scroll positioning math (see comment in doSend). */}
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
                if (listeningRef.current) return  // block Chrome direct-fill during recording
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
