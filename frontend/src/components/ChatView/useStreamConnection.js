import { useState, useEffect, useRef, useCallback } from 'react'
import { getToken, BASE } from '../../api/client.js'
import { questionKey } from './questionKey.js'

// Characters revealed per frame at 60fps.
// 3 chars/frame × 60fps = ~180 chars/sec — fast but smooth.
// DO NOT increase beyond 5 — it defeats the typewriter effect.
// DO NOT decrease below 2 — it makes streaming feel sluggish.
const CHARS_PER_FRAME = 3

// Window during which a 204 from /stream after a send is a race
// (the SSE GET landed before chats_stream.py:POST /messages finished
// registering the broadcast) rather than "agent finished." The POST
// handler returns 202 only AFTER create_broadcast(chat_id) completes,
// so any 204 outside this window genuinely means there's no active
// turn left and the right move is a DB refresh. Inside the window,
// schedule a quick reconnect instead — refreshing here would wipe
// the optimistic user message before persistence catches up.
//
// 1.5s is the empirical headroom: round-trip + create_broadcast +
// scheduler hop are well under that on local + remote prod traffic.
const BROADCAST_REGISTRATION_WINDOW_MS = 1500

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
 * requestAnimationFrame for a smooth typewriter effect. Tool events
 * and non-text events are applied immediately.
 *
 * SSE event vocabulary — what the backend producers (see
 * `backend/app/chat.py` + runners) send, and how this hook handles
 * each. Adding a new event type requires editing BOTH the backend
 * emitter AND the dispatch switch below in this file.
 *
 *   text                  Streamed assistant token chunk
 *                         { content }. Buffered + drained by rAF.
 *   tool_start            Tool invocation began
 *                         { tool, input? }. Appends a tool block
 *                         with status='running'.
 *   tool_input            Backfill summary (assistant block arrived
 *                         after start). { input }.
 *   tool_output           Tool finished, here's its result
 *                         { content }. Attaches to running block.
 *   tool_end              Marks the running tool done (status flip).
 *   question              AskUserQuestion fired
 *                         { questions: [...] }. Renders a card.
 *   queued_turn_starting  Backend about to promote a queued message
 *                         { ts }. Notifies caller via callback.
 *   catch_up_done         Replay burst finished; live events follow.
 *   error                 { message }. Surfaced inline.
 *   done                  Turn complete; SSE closes.
 *
 * System events (theme_updated, app_updated, shell_rebuilding,
 * shell_rebuilt, shell_rebuild_failed) listed in the `SYSTEM_EVENTS`
 * set above are forwarded to `onSystemEvent` instead of touching
 * `streamItems` — they aren't chat content.
 *
 * @param {string} chatId
 * @param {object} callbacks
 * @param {(info?: {continues: boolean, promotedTs: number|null}) => void} [callbacks.onStreamEnd]
 *   Fired one rAF after the `done` event so React commits before the
 *   caller promotes streamItems to messages.
 * @param {(event: object) => void} [callbacks.onSystemEvent]
 *   Fired for non-chat SSE events (theme/app/shell). Not buffered.
 * @param {(opts?: {force?: boolean}) => void} [callbacks.onNeedsRefresh]
 *   Fired when the stream returns 204 outside the post-send race
 *   window — caller should refetch persisted DB state.
 * @param {(ts: number|null) => void} [callbacks.onQueuedTurnStarting]
 *   Fired when the backend is about to promote a queued message; `ts`
 *   identifies which pending entry was promoted.
 *
 * @returns {{
 *   streamItems: Array<
 *     | {type: 'text', content: string}
 *     | {type: 'tool', tool: string, input: string, output: string,
 *        status: 'running' | 'done'}
 *     | {type: 'question', questions: Array<object>}
 *     | {type: 'error', message: string}
 *   >,
 *   latestItemsRef: React.MutableRefObject<Array<object>>,
 *   isStreaming: boolean,
 *   isStreamingRef: React.MutableRefObject<boolean>,
 *   connectionError: string | null,
 *   sendMessage: (text: string, attachments?: Array<object>,
 *                 opts?: {hidden?: boolean, queueOnly?: boolean,
 *                         answers?: object}) => Promise<object>,
 *   connectToStream: () => void,
 *   retry: () => void,
 *   disconnect: (opts?: {clearStreaming?: boolean}) => void,
 *   clearStreamItems: () => void,
 * }}
 *
 * `isStreamingRef` is the synchronous mirror of `isStreaming`. It
 * exists because ChatView's `handleStop` reads it from a closure
 * that crosses a render boundary (after the `/chat/stop` await),
 * and the queue-vs-fresh-send guard in `doSend` reads it from a
 * callback fired during `setSending`'s commit window. Both paths
 * need the latest value RIGHT NOW, not the value captured at last
 * render — so the ref is load-bearing, not a convenience.
 */
export default function useStreamConnection(chatId, {
  onStreamEnd,
  onSystemEvent,
  onNeedsRefresh,
  onQueuedTurnStarting,
}) {
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

  const [isStreaming, _setIsStreaming] = useState(false)
  const isStreamingRef = useRef(false)
  function setIsStreaming(v) { isStreamingRef.current = v; _setIsStreaming(v) }
  const [connectionError, _setConnectionError] = useState(null)
  const connectionErrorRef = useRef(null)
  function setConnectionError(v) { connectionErrorRef.current = v; _setConnectionError(v) }

  const abortRef = useRef(null)
  const retryCount = useRef(0)
  const chatIdRef = useRef(chatId)
  chatIdRef.current = chatId

  // Tracks setTimeout handles for reconnect attempts so unmount can
  // cancel them. Without this, a timer scheduled by the SSE loop can
  // fire after unmount and call connectRef.current — which then
  // setState on a dead component (React warning + potential leak).
  const reconnectTimersRef = useRef(new Set())
  function scheduleReconnect(fn, delay) {
    const handle = setTimeout(() => {
      reconnectTimersRef.current.delete(handle)
      fn()
    }, delay)
    reconnectTimersRef.current.add(handle)
    return handle
  }
  function cancelReconnectTimers() {
    for (const h of reconnectTimersRef.current) clearTimeout(h)
    reconnectTimersRef.current.clear()
  }

  // Timestamp of the most recent sendMessage call. A 204 from /stream
  // shortly after a send means the broadcast hasn't been registered yet
  // — not that the agent already finished. Distinguishing these
  // prevents an onNeedsRefresh → fetchMessages race that wipes the
  // optimistic user message before the DB persists it.
  const justSentAtRef = useRef(0)

  // Max innerHeight ever observed for this session. Used as the viewport
  // height sent to the backend (which forwards it to the agent so its
  // screenshots match the partner's framing). interactive-widget=
  // resizes-content shrinks innerHeight when the keyboard opens — and
  // POST /messages typically fires WITH the keyboard open — so the raw
  // value is keyboard-poisoned. Max-tracking mirrors the same defensive
  // trick useScrollMode applies to the spacer (fullViewHRef).
  const maxInnerHeightRef = useRef(
    typeof window !== 'undefined' ? window.innerHeight : 0
  )

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

  const disconnect = useCallback(({ clearStreaming = false } = {}) => {
    // Salvage any text that's in the typewriter buffer but hasn't
    // been drained into streamItems yet. Without this, Stop loses
    // the most recent text chunks (up to ~1 frame of buffered
    // characters) because the drain rAF is cancelled below before it
    // gets to push them. promoteStreamToMessages reads latestItemsRef
    // — if the buffer didn't land, those chars are gone forever.
    //
    // flushBuffer is also called by the `done` event handler before
    // teardown, so this is redundant on the happy path but
    // load-bearing on abort/Stop. Idempotent — does nothing if the
    // buffer is empty.
    flushBuffer()
    if (abortRef.current) {
      abortRef.current.abort()
      abortRef.current = null
    }
    drainingRef.current = false
    cancelReconnectTimers()
    if (clearStreaming) {
      setIsStreaming(false)
      setConnectionError(null)
      retryCount.current = 0
      justSentAtRef.current = 0
    }
  }, [flushBuffer])

  useEffect(() => () => disconnect(), [chatId, disconnect])

  useEffect(() => {
    function trackMaxHeight() {
      if (window.innerHeight > maxInnerHeightRef.current) {
        maxInnerHeightRef.current = window.innerHeight
      }
    }
    trackMaxHeight()
    window.addEventListener('resize', trackMaxHeight)
    return () => window.removeEventListener('resize', trackMaxHeight)
  }, [])

  // Use a ref for connectToStream so handleReconnect always
  // calls the latest version (avoids stale closure).
  const connectRef = useRef(null)
  const onStreamEndRef = useRef(onStreamEnd)
  onStreamEndRef.current = onStreamEnd
  const onSystemEventRef = useRef(onSystemEvent)
  onSystemEventRef.current = onSystemEvent
  const onNeedsRefreshRef = useRef(onNeedsRefresh)
  onNeedsRefreshRef.current = onNeedsRefresh
  const onQueuedTurnStartingRef = useRef(onQueuedTurnStarting)
  onQueuedTurnStartingRef.current = onQueuedTurnStarting
  const queuedContinuationRef = useRef(false)
  // Carries the ts of the message the backend just promoted so the
  // frontend can remove the matching pending entry, even if the user
  // canceled or reordered items locally in the meantime.
  const queuedContinuationTsRef = useRef(null)

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
        if (sinceSend < BROADCAST_REGISTRATION_WINDOW_MS) {
          abortRef.current = null
          scheduleReconnect(() => connectRef.current?.(false), 300)
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
        // Let ChatView clear its `sending` state, then force a refresh
        // from persisted DB state. This path is terminal: there is no
        // active broadcast left to clobber.
        requestAnimationFrame(() => {
          onStreamEndRef.current?.()
          onNeedsRefreshRef.current?.({ force: true })
        })
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
          } else if (event.type === 'question') {
            const questions = event.questions || []
            if (questions.length > 0 && questions[0]?.question) {
              flushBuffer()
              // Coalesce by stable identity (question id, falling back
              // to text). Adjacency-based dedup ("last item is
              // question?") left phantom cards behind whenever a text
              // token or tool boundary landed between two partial
              // deliveries for the same AskUserQuestion call. Mirrors
              // backend/app/events.py:process_event so the SSE stream
              // and the persisted message agree on identity.
              const incoming = { type: 'question', questions }
              const key = questionKey(incoming)
              setStreamItems(prev => {
                const idx = prev.findIndex(
                  it => it.type === 'question' && questionKey(it) === key
                )
                if (idx !== -1) {
                  const updated = [...prev]
                  updated[idx] = incoming
                  return updated
                }
                return [...prev, incoming]
              })
            }
          } else if (event.type === 'error') {
            flushBuffer()
            setStreamItems(prev => {
              const updated = prev.map(b =>
                b.type === 'tool' && b.status === 'running'
                  ? { ...b, status: 'done' } : b
              )
              // Use the same `error` block shape the backend
              // persists, so MsgContent renders it identically
              // before and after promote — without this the
              // streaming "Error: ..." text was a plain text block
              // and the post-promote DB block was `{type:'error'}`,
              // and the latter rendered as null because MsgContent
              // had no branch for it (which is the bug we're
              // fixing — the error visibly disappeared on chat
              // return).
              updated.push({ type: 'error', message: event.message })
              return updated
            })
          } else if (event.type === 'queued_turn_starting') {
            queuedContinuationRef.current = true
            queuedContinuationTsRef.current = event.ts ?? null
            onQueuedTurnStartingRef.current?.(event.ts)
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
            const continues = queuedContinuationRef.current
            const promotedTs = queuedContinuationTsRef.current
            queuedContinuationRef.current = false
            queuedContinuationTsRef.current = null
            // Delay onStreamEnd by one frame so the React render
            // triggered by setIsStreaming(false) above completes before
            // ChatView promotes streamItems to messages.  latestItemsRef
            // is already up-to-date (synchronous), so this is purely for
            // render consistency (e.g. streaming UI teardown).
            requestAnimationFrame(() => {
              onStreamEndRef.current?.({ continues, promotedTs })
              if (continues) {
                scheduleReconnect(() => connectRef.current?.(true), 150)
              }
            })
            return
          } else if (import.meta.env.DEV) {
            // Surface event types the dispatch chain doesn't handle
            // so a new SDK-runner event doesn't get silently dropped
            // during development. Prod stays silent.
            console.debug(
              'useStreamConnection: unknown event type', event.type, event,
            )
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
        scheduleReconnect(() => connectRef.current?.(true), delay)
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

  const sendMessage = useCallback(async (
    text,
    attachments,
    { hidden = false, queueOnly = false, answers = undefined } = {},
  ) => {
    // Answer submissions (hidden+answers) ride the EXISTING turn —
    // the runner is paused on the AskUserQuestion future and resumes
    // in place. Wiping streamItems here would erase the question card
    // the user just answered, and the post-answer agent output would
    // render as if a fresh turn started (no context for what the user
    // is replying to). Keep streamItems intact for the answer path.
    const isAnswerSubmission = !!answers
    if (!queueOnly && !isAnswerSubmission) {
      justSentAtRef.current = Date.now()
      setStreamItems([])
      textBufferRef.current = ''
      setIsStreaming(true)
      setConnectionError(null)
    }

    try {
      const body = { content: text }
      if (hidden) body.hidden = true
      // AskUserQuestion answers persist atomically with the hidden
      // user message — backend writes them into the question block
      // in the same transaction (see chats_stream.py).
      if (answers) body.answers = answers
      if (attachments && attachments.length > 0) {
        body.attachments = attachments
      }
      try { body.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone } catch {}
      body.viewport = {
        width: window.innerWidth,
        height: maxInnerHeightRef.current || window.innerHeight,
      }
      const res = await fetch(`${BASE}/api/chats/${chatIdRef.current}/messages`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${getToken()}`,
        },
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      // Trust the backend's actual status, not the frontend's queueOnly
      // hint. The frontend's `sending` flag can be stale (turn finished
      // between the doSend check and the POST landing), so a request
      // sent with queueOnly:true can come back as "started". Always
      // connect to the stream when the backend says it started.
      if (data.status === 'queued') return data
      // AskUserQuestion answer was delivered in-process to the parked
      // future — the runner resumes the EXISTING turn with the answer.
      // No new stream connection needed; the existing SSE keeps
      // streaming whatever the runner emits next. Returning here
      // prevents a redundant reconnect that would close the live
      // stream and replay the full catch-up burst.
      if (data.status === 'answer_delivered') return data
      // Started: ensure streaming state is set even if the caller
      // passed queueOnly:true expecting it would be queued.
      if (queueOnly) {
        justSentAtRef.current = Date.now()
        setStreamItems([])
        textBufferRef.current = ''
        setIsStreaming(true)
        setConnectionError(null)
      }
    } catch (err) {
      if (!queueOnly) setIsStreaming(false)
      throw err
    }

    // No delay needed: chats_stream.py's POST handler calls
    // create_broadcast(chat_id) synchronously BEFORE returning 202,
    // so by the time this code resumes after `await apiFetch(...)`
    // the broadcast is registered. The GET /stream we're about to
    // make can find it. (The previous 50ms wait was a patch around
    // a misdiagnosed race; verified deterministic by inspecting
    // backend/app/routes/chats_stream.py:121-131.)
    connectRef.current?.(true)
    return { status: 'started' }
  }, [])

  // Reconnect on visibility change or network recovery, but ONLY
  // when we believe a stream is active (isStreamingRef). Idle chats
  // are left alone — no pointless 204 + DB refetch on every tab
  // switch.
  //
  // Two cases on wake while streaming:
  // (a) Connection cleanly closed while backgrounded (abortRef is
  //     null, isStreaming still true) → reconnect with reset.
  // (b) Connection died silently — TCP dropped during sleep, no
  //     error event fired, abortRef still non-null. We abort the
  //     stale controller and reconnect; the catch-up burst replays
  //     everything the client missed.
  //
  // Without this, the UI shows frozen "thinking" dots forever
  // after screen-lock during streaming.
  useEffect(() => {
    function onVisible() {
      if (document.visibilityState !== 'visible') return
      if (!isStreamingRef.current) return
      if (abortRef.current) {
        abortRef.current.abort()
        abortRef.current = null
      }
      connectRef.current?.(true)
    }

    function onOnline() {
      if (!isStreamingRef.current && !connectionErrorRef.current) return
      if (abortRef.current) {
        abortRef.current.abort()
        abortRef.current = null
      }
      connectRef.current?.(true)
    }

    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('online', onOnline)

    return () => {
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('online', onOnline)
    }
  }, [])

  // Exposed so ChatView's promoteStreamToMessages can wipe the live
  // streamItems right after copying them into `messages`. Without this,
  // the conditional `<li>` rendering `streamItems` (gated on `sending`,
  // which stays true through a queued continuation) double-renders the
  // just-promoted content for ~150ms until reconnect calls
  // setStreamItems([]) — the user sees a duplicate of the assistant
  // message that flashes and disappears.
  function clearStreamItems() {
    setStreamItems([])
    textBufferRef.current = ''
  }

  return {
    streamItems,
    latestItemsRef,
    isStreaming,
    isStreamingRef,
    connectionError,
    sendMessage,
    connectToStream,
    retry,
    disconnect,
    clearStreamItems,
  }
}
