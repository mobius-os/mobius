import { useState, useEffect, useRef, useCallback } from 'react'
import { getToken, BASE } from '../../api/client.js'

// Characters revealed per frame at 60fps.
// 3 chars/frame × 60fps = ~180 chars/sec — fast but smooth.
// DO NOT increase beyond 5 — it defeats the typewriter effect.
// DO NOT decrease below 2 — it makes streaming feel sluggish.
const CHARS_PER_FRAME = 3

// Events that come through the chat SSE stream but are not chat content.
// These are published by agent scripts (notify_theme.sh, register_app.py,
// rebuild_shell.sh) via POST /api/notify, which pushes them into the
// active ChatBroadcast.  The SSE stream delivers them alongside normal
// chat events.  We forward them to the onSystemEvent callback rather
// than processing them as text/tool events.
const SYSTEM_EVENTS = new Set([
  'theme_updated',
  'app_updated',
  'shell_rebuilding',
  'shell_rebuilt',
  'shell_rebuild_failed',
])

/**
 * Hook that manages an SSE connection to /api/chats/{chatId}/stream.
 *
 * Text tokens are buffered and released character-by-character via
 * requestAnimationFrame for a smooth typewriter effect.  Tool events
 * and non-text events are applied immediately.
 */
export default function useStreamConnection(chatId, { onStreamEnd, onSystemEvent, onNeedsRefresh }) {
  const [streamItems, _setStreamItems] = useState([])
  const latestItemsRef = useRef([])

  // Wrapper that keeps latestItemsRef in sync synchronously.
  // This prevents promoteStreamToMessages from reading stale items
  // when called from a requestAnimationFrame that fires before React
  // processes a queued state updater.
  function setStreamItems(updater) {
    const next = typeof updater === 'function' ? updater(latestItemsRef.current) : updater
    latestItemsRef.current = next
    _setStreamItems(next)
  }

  const [isStreaming, setIsStreaming] = useState(false)
  const [connectionError, setConnectionError] = useState(null)

  const abortRef = useRef(null)
  const retryCount = useRef(0)
  const chatIdRef = useRef(chatId)
  chatIdRef.current = chatId

  // Timestamp of the most recent sendMessage call. A 204 from /stream
  // shortly after a send means the broadcast hasn't been registered yet
  // — not that the agent already finished. Distinguishing these
  // prevents an onNeedsRefresh → fetchMessages race that wipes the
  // optimistic user message before the DB persists it.
  const justSentAtRef = useRef(0)

  // Character buffer for smooth text reveal.
  const textBufferRef = useRef('')
  const rafRef = useRef(null)
  const drainingRef = useRef(false)

  // Drain the text buffer character-by-character.
  const startDraining = useCallback(() => {
    if (drainingRef.current) return
    drainingRef.current = true

    function drain() {
      const buf = textBufferRef.current
      if (buf.length === 0) {
        drainingRef.current = false
        return
      }

      const chunk = buf.slice(0, CHARS_PER_FRAME)
      textBufferRef.current = buf.slice(CHARS_PER_FRAME)

      setStreamItems(prev => {
        const updated = [...prev]
        const last = updated[updated.length - 1]
        if (last?.type === 'text') {
          updated[updated.length - 1] = {
            ...last, content: last.content + chunk,
          }
        } else {
          updated.push({ type: 'text', content: chunk })
        }
        return updated
      })

      rafRef.current = requestAnimationFrame(drain)
    }

    rafRef.current = requestAnimationFrame(drain)
  }, [])

  // Flush all remaining buffer immediately.
  // IMPORTANT: always cancel rAF even if buffer looks empty — the drain
  // loop may have just taken the last chars and its setStreamItems hasn't
  // been processed yet.  Without this, promoteStreamToMessages() in
  // ChatView can read stale state and drop the final characters.
  const flushBuffer = useCallback(() => {
    if (rafRef.current) {
      cancelAnimationFrame(rafRef.current)
      rafRef.current = null
    }
    drainingRef.current = false

    const remaining = textBufferRef.current
    if (!remaining) return
    textBufferRef.current = ''

    setStreamItems(prev => {
      const updated = [...prev]
      const last = updated[updated.length - 1]
      if (last?.type === 'text') {
        updated[updated.length - 1] = {
          ...last, content: last.content + remaining,
        }
      } else {
        updated.push({ type: 'text', content: remaining })
      }
      return updated
    })
  }, [])

  const disconnect = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort()
      abortRef.current = null
    }
    if (rafRef.current) {
      cancelAnimationFrame(rafRef.current)
      rafRef.current = null
    }
    drainingRef.current = false
  }, [])

  useEffect(() => () => disconnect(), [chatId, disconnect])

  // Use a ref for connectToStream so handleReconnect always
  // calls the latest version (avoids stale closure).
  const connectRef = useRef(null)
  const onStreamEndRef = useRef(onStreamEnd)
  onStreamEndRef.current = onStreamEnd
  const onSystemEventRef = useRef(onSystemEvent)
  onSystemEventRef.current = onSystemEvent
  const onNeedsRefreshRef = useRef(onNeedsRefresh)
  onNeedsRefreshRef.current = onNeedsRefresh

  const connectToStream = useCallback(async (resetState = false) => {
    disconnect()

    if (resetState) {
      setStreamItems([])
      textBufferRef.current = ''
    }

    const controller = new AbortController()
    abortRef.current = controller

    try {
      const res = await fetch(`${BASE}/api/chats/${chatIdRef.current}/stream`, {
        headers: { Authorization: `Bearer ${getToken()}` },
        signal: controller.signal,
      })

      if (res.status === 204) {
        // A 204 within ~1.5s of sendMessage means the broadcast hasn't
        // been registered yet (POST→GET race inside the same event
        // loop), not that the agent already finished. Schedule a
        // reconnect instead of refreshing from the DB — a DB refresh
        // here would overwrite the optimistic user message before the
        // backend has finished persisting it.
        const sinceSend = Date.now() - justSentAtRef.current
        if (sinceSend < 1500) {
          abortRef.current = null
          setTimeout(() => connectRef.current?.(false), 300)
          return
        }

        // No active stream — the broadcast is gone, which means either
        // the agent never started on this chat or it already finalized
        // and saved the response to the DB.  In both cases the right
        // move is to DROP any stale partial items we may still hold
        // from a previous connection.  Promoting them would duplicate
        // whatever the DB fetch is about to return.  Null the
        // controller so visibility/online handlers can fire future
        // reconnections.
        abortRef.current = null
        setConnectionError(null)
        retryCount.current = 0
        setIsStreaming(false)
        setStreamItems([])
        textBufferRef.current = ''
        // The chat may have finished while we were offline — re-fetch
        // messages from the DB so the component shows the final state.
        onNeedsRefreshRef.current?.()
        return
      }

      if (!res.ok) throw new Error(`HTTP ${res.status}`)

      setIsStreaming(true)
      setConnectionError(null)
      retryCount.current = 0

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      // The first reader.read() returns the catch-up burst as one chunk.
      // Apply catch-up text immediately (no typewriter animation).
      let isCatchUp = true

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          let event
          try { event = JSON.parse(line.slice(6)) } catch { continue }

          if (SYSTEM_EVENTS.has(event.type)) {
            onSystemEventRef.current?.(event)
            continue
          }

          if (event.type === 'catch_up_done') {
            isCatchUp = false
            continue
          }

          if (event.type === 'text') {
            const content = event.content || ''
            if (isCatchUp) {
              // Catch-up burst: apply immediately, no typewriter.
              setStreamItems(prev => {
                const updated = [...prev]
                const last = updated[updated.length - 1]
                if (last?.type === 'text') {
                  updated[updated.length - 1] = {
                    ...last, content: last.content + content,
                  }
                } else {
                  updated.push({ type: 'text', content })
                }
                return updated
              })
            } else {
              // Live streaming: buffer for typewriter reveal.
              textBufferRef.current += content
              startDraining()
            }
          } else if (event.type === 'tool_start') {
            flushBuffer()
            setStreamItems(prev => [...prev, {
              type: 'tool',
              tool: event.tool,
              input: event.input || '',
              output: '',
              status: 'running',
            }])
          } else if (event.type === 'tool_input') {
            // Backfill input summary from the assistant event.
            // Match earliest tool without input (same order as assistant event).
            setStreamItems(prev => {
              const updated = [...prev]
              const i = updated.findIndex(
                b => b.type === 'tool' && !b.input
              )
              if (i !== -1) updated[i] = { ...updated[i], input: event.input }
              return updated
            })
          } else if (event.type === 'tool_output') {
            setStreamItems(prev => {
              const updated = [...prev]
              const rev = [...updated].reverse()
              const ri = rev.findIndex(b => b.type === 'tool' && b.status === 'running')
              const i = ri === -1 ? -1 : updated.length - 1 - ri
              if (i !== -1) updated[i] = { ...updated[i], output: event.content }
              return updated
            })
          } else if (event.type === 'tool_end') {
            setStreamItems(prev => {
              const updated = [...prev]
              const rev = [...updated].reverse()
              const ri = rev.findIndex(b => b.type === 'tool' && b.status === 'running')
              const i = ri === -1 ? -1 : updated.length - 1 - ri
              if (i !== -1) updated[i] = { ...updated[i], status: 'done' }
              return updated
            })
          } else if (event.type === 'error') {
            flushBuffer()
            setStreamItems(prev => {
              const updated = prev.map(b =>
                b.type === 'tool' && b.status === 'running'
                  ? { ...b, status: 'done' } : b
              )
              updated.push({ type: 'text', content: `\n\nError: ${event.message}` })
              return updated
            })
          } else if (event.type === 'done') {
            flushBuffer()
            setIsStreaming(false)
            // Null the controller so visibility/online handlers know the
            // connection is closed and can trigger a reconnect if the user
            // backgrounds and returns.  Without this, abortRef stays
            // non-null after a normal stream completion and the onVisible
            // guard (!abortRef.current) prevents reconnection.
            abortRef.current = null
            // Close the just-sent race window — the stream completed
            // normally, so any subsequent 204 genuinely means the chat
            // is finished (and should trigger a DB refresh) rather than
            // a POST→GET race needing a retry.
            justSentAtRef.current = 0
            // Delay onStreamEnd by one frame so the React render
            // triggered by setIsStreaming(false) above completes before
            // ChatView promotes streamItems to messages.  latestItemsRef
            // is already up-to-date (synchronous), so this is purely for
            // render consistency (e.g. streaming UI teardown).
            requestAnimationFrame(() => onStreamEndRef.current?.())
            return
          }
        }
      }

      // Stream closed without done event.
      abortRef.current = null
      flushBuffer()
      setIsStreaming(false)
      requestAnimationFrame(() => onStreamEndRef.current?.())
    } catch (err) {
      if (err.name === 'AbortError') return
      flushBuffer()
      setIsStreaming(false)
      // Retry with exponential backoff.
      // IMPORTANT: reconnect with resetState=true so streamItems are cleared
      // before the catch-up burst.  Without this, catch-up replays all events
      // from the start and appends them on top of existing streamItems,
      // duplicating the initial portion of the response.
      if (retryCount.current >= 3) {
        setConnectionError('disconnected')
        // Null the stale controller so visibility/online handlers can
        // trigger reconnection after retries exhaust.
        abortRef.current = null
      } else {
        setConnectionError('retrying')
        const delay = Math.pow(2, retryCount.current) * 1000
        retryCount.current++
        setTimeout(() => connectRef.current?.(true), delay)
      }
    }
  }, [disconnect, startDraining, flushBuffer])

  // Keep ref in sync so retry timeouts call the latest version.
  connectRef.current = connectToStream

  const retry = useCallback(() => {
    retryCount.current = 0
    setConnectionError(null)
    setIsStreaming(true)
    // Reset state — same reason as the automatic retry above.
    connectRef.current?.(true)
  }, [])

  const sendMessage = useCallback(async (text, attachments) => {
    justSentAtRef.current = Date.now()
    setStreamItems([])
    textBufferRef.current = ''
    setIsStreaming(true)
    setConnectionError(null)

    try {
      const body = { content: text }
      if (attachments && attachments.length > 0) {
        body.attachments = attachments
      }
      try { body.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone } catch {}
      body.viewport = { width: window.innerWidth, height: window.innerHeight }
      const res = await fetch(`${BASE}/api/chats/${chatIdRef.current}/messages`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${getToken()}`,
        },
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
    } catch (err) {
      setIsStreaming(false)
      throw err
    }

    // Small delay for the background task to start emitting events.
    await new Promise(r => setTimeout(r, 50))
    connectRef.current?.(true)
  }, [])

  // Reconnect on visibility change or network recovery.
  // The !abortRef.current guard prevents firing during active streaming
  // (the controller exists while connected).  When it does fire (e.g.
  // connection was lost while backgrounded), we reset state so the
  // catch-up burst doesn't duplicate existing streamItems.
  useEffect(() => {
    function onVisible() {
      if (document.visibilityState === 'visible' && !abortRef.current) {
        connectRef.current?.(true)
      }
    }

    function onOnline() {
      if (!abortRef.current) {
        connectRef.current?.(true)
      }
    }

    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('online', onOnline)

    return () => {
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('online', onOnline)
    }
  }, [])

  return {
    streamItems,
    latestItemsRef,
    isStreaming,
    connectionError,
    sendMessage,
    connectToStream,
    retry,
    disconnect,
  }
}
