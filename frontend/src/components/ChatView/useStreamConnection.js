import { useState, useEffect, useRef, useCallback } from 'react'
import { getToken, BASE } from '../../api/client.js'
import { questionKey } from './questionKey.js'
import {
  upsertQuestionItem,
  attachToolOutput,
  closeToolLifecycle,
  closeAllToolLifecycles,
} from './streamReducers.js'

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
  // app_built is the chat-SCOPED CTA signal: the backend publishes it
  // onto ONLY the building chat's broadcast (see routes/notify.py), so
  // it arrives exclusively on the stream of the chat that built the app.
  // Forwarded to onSystemEvent like the other system events; the handler
  // sets the "Open app" CTA. (app_updated stays list-refresh-only.)
  'app_built',
  'shell_rebuilding',
  'shell_rebuilt',
  'shell_rebuild_failed',
  'chat_run_started',
  'chat_run_finished',
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
 *   text_boundary         Next text starts a fresh assistant block.
 *                         Used when the provider starts a new assistant
 *                         message item without a visible tool between them.
 *   tool_start            Tool invocation began
 *                         { tool, input? }. Appends a tool block
 *                         with status='running'.
 *   tool_input            Backfill summary (assistant block arrived
 *                         after start). { input }.
 *   tool_output           Tool finished, here's its result
 *                         { content }. Attaches to running block.
 *   tool_end              Marks the running tool done (status flip).
 *   skill_loaded          Agent loaded a skill { skill }. Stamps the
 *                         name onto the matching Skill tool block so
 *                         ToolBlock renders a chip.
 *   question              AskUserQuestion fired
 *                         { question_id, questions: [...] }. Renders a
 *                         card, absorbing the call's own tool_start
 *                         block in place (see streamReducers.js).
 *   queued_turn_starting  Backend about to promote a queued message
 *                         { ts }. Notifies caller via callback.
 *   steered_into_turn     A send was steered into a live provider turn
 *                         instead of queued { ts, content }. Codex uses
 *                         true SDK steer; Claude interrupts and
 *                         re-prompts. Notifies caller so it drops the
 *                         optimistic queued-tray entry and renders the
 *                         message inline as content growth.
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
 * @param {(info: {ts: number|null, message: object|null}) => void} [callbacks.onQueuedTurnStarting]
 *   Fired when the backend is about to promote queued follow-ups; `ts`
 *   identifies the first pending entry in the promoted group and `message`
 *   is the backend-authoritative combined user message when available.
 * @param {(info: {ts: number|null, content: string}) => void} [callbacks.onSteeredIntoTurn]
 *   Fired when a send was steered into a live provider turn. The caller
 *   drops the optimistic queued-tray entry and renders the message inline.
 * @param {(questionId: string|null) => void} [callbacks.onLiveQuestion]
 *   Fired when the stream shows the currently-live AskUserQuestion card.
 *
 * @returns {{
 *   streamItems: Array<
 *     | {type: 'text', content: string}
 *     | {type: 'tool', tool: string, input: string, output: string,
 *        status: 'running' | 'done'}
 *     | {type: 'question', questions: Array<object>,
 *        question_id?: string, answers?: object, absorbedTool?: string}
 *     | {type: 'error', message: string}
 *   >,
 *   latestItemsRef: React.MutableRefObject<Array<object>>,
 *   isStreaming: boolean,
 *   isStreamingRef: React.MutableRefObject<boolean>,
 *   connectionError: string | null,
 *   sendMessage: (text: string, attachments?: Array<object>,
 *                 opts?: {hidden?: boolean, queueOnly?: boolean,
 *                         forceSteer?: boolean, consumePendingTs?: number[],
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
  onSteeredIntoTurn,
  onLiveQuestion,
}) {
  const [streamItems, _setStreamItems] = useState([])
  const latestItemsRef = useRef([])
  // Snapshot of the last non-empty latestItemsRef value. Written every
  // time items become non-empty; never cleared by a reconnect reset.
  // On retry exhaustion we promote from this ref so the user sees the
  // partial response instead of a blank screen.
  //
  // CONTRACT: updated before any reconnect wipes latestItemsRef.
  // Only wiped when a successful catch-up burst begins (items actually
  // arrive on the new connection), preventing stale partial from
  // doubling into a complete response on a successful reconnect.
  const lastGoodItemsRef = useRef([])
  // Set to true from the first event of a catch-up burst on a new
  // connection. Once we see real events, lastGoodItemsRef can be
  // cleared; if retries exhaust before this flag fires, we still have
  // the last-good snapshot.
  const catchUpStartedRef = useRef(false)

  // Wrapper that keeps latestItemsRef in sync synchronously.
  // This prevents promoteStreamToMessages from reading stale items
  // when called from a requestAnimationFrame that fires before React
  // processes a queued state updater.
  function setStreamItems(updater) {
    const next = typeof updater === 'function' ? updater(latestItemsRef.current) : updater
    if (next.length > 0) lastGoodItemsRef.current = next
    latestItemsRef.current = next
    _setStreamItems(next)
  }

  const [isStreaming, _setIsStreaming] = useState(false)
  const isStreamingRef = useRef(false)
  function setIsStreaming(v) { isStreamingRef.current = v; _setIsStreaming(v) }
  // Reconnect intent is deliberately separate from `isStreaming`.
  // `isStreaming` drives UI state and can be disturbed by browser
  // sleep/wake network EOFs. This ref tracks whether the current turn
  // has seen a terminal signal yet. Idle chats only reconnect when
  // this is true; `done`, terminal 204, and Stop retire it.
  const wantsReconnectRef = useRef(false)
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
  // Set by `text_boundary`: the next text chunk must create a new text item
  // instead of appending to the previous text item. This preserves provider
  // assistant-message boundaries even when the separator was hidden/internal
  // work rather than a visible tool block.
  const forceNewTextBlockRef = useRef(false)

  function appendTextChunk(prev, chunk) {
    const updated = [...prev]
    const last = updated[updated.length - 1]
    if (last?.type === 'text' && !forceNewTextBlockRef.current) {
      updated[updated.length - 1] = {
        ...last, content: last.content + chunk,
      }
    } else {
      updated.push({ type: 'text', content: chunk })
    }
    forceNewTextBlockRef.current = false
    return updated
  }

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

      setStreamItems(prev => appendTextChunk(prev, chunk))

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

    setStreamItems(prev => appendTextChunk(prev, remaining))
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
      wantsReconnectRef.current = false
      setIsStreaming(false)
      setConnectionError(null)
      retryCount.current = 0
      justSentAtRef.current = 0
    }
  }, [flushBuffer])

  useEffect(() => () => {
    // On chatId change: tear down the old connection AND wipe stream
    // state. Without the wipe, streamItems from the previous chat
    // survive into the new chat's mount: connectToStream(false) passes
    // resetState=false so the catch-up burst appends onto the survivor
    // items, and the user sees ghost content from the previous chat.
    // The bridge-partial bridge logic is NOT affected — useBridgePartial
    // captures its ts from the initial DB fetch, not from streamItems;
    // clearing streamItems here does not interact with that gate.
    disconnect()
    setStreamItems([])
    textBufferRef.current = ''
    forceNewTextBlockRef.current = false
    lastGoodItemsRef.current = []
    // Answers belong to the chat we're leaving; carrying them into the next
    // chat could re-arm a same-keyed question with a foreign answer.
    answersByQuestionKeyRef.current.clear()
  }, [chatId, disconnect])

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
  const onSteeredIntoTurnRef = useRef(onSteeredIntoTurn)
  onSteeredIntoTurnRef.current = onSteeredIntoTurn
  const onLiveQuestionRef = useRef(onLiveQuestion)
  onLiveQuestionRef.current = onLiveQuestion
  const queuedContinuationRef = useRef(false)
  // Carries the ts of the message the backend just promoted so the
  // frontend can remove the matching pending entry, even if the user
  // canceled or reordered items locally in the meantime.
  const queuedContinuationTsRef = useRef(null)
  const queuedContinuationMessageRef = useRef(null)

  // Answers the user submitted this turn, keyed by questionKey. The SSE
  // catch-up burst re-emits the original `question` event WITHOUT answers
  // (they live in chat.messages, not in the per-turn event stream), and
  // every reconnect path wipes streamItems before that replay — so
  // upsertQuestionItem's prev-item answer-carry has nothing to carry from
  // and the replayed card renders back to PENDING, visibly reverting the
  // user's answer mid-turn. This ref outlives the wipe: patchQuestionAnswers
  // records the answer here, and the `question` handler re-arms each incoming
  // event from it before upserting. Cleared on chatId change and at turn
  // `done` (the answer is durable in the promoted message by then).
  const answersByQuestionKeyRef = useRef(new Map())

  const connectToStream = useCallback(async (resetState = false) => {
    disconnect()

    if (resetState) {
      // Clear visible state but preserve lastGoodItemsRef — that snapshot
      // survives until we confirm the new connection is delivering events.
      // Wiping latestItemsRef here is safe for a SUCCESSFUL reconnect
      // because the catch-up burst will replay everything; it's only
      // unsafe on retry exhaustion where no events arrive. catchUpStarted
      // tracks whether events arrived on this connection attempt.
      catchUpStartedRef.current = false
      setStreamItems([])
      textBufferRef.current = ''
      forceNewTextBlockRef.current = false
    }

    const controller = new AbortController()
    abortRef.current = controller

    try {
      const res = await fetch(`${BASE}/api/chats/${chatIdRef.current}/stream`, {
        headers: { Authorization: `Bearer ${getToken()}` },
        signal: controller.signal,
      })

      // Stale-connection guard. Between scheduling this fetch and its
      // resolution, the connection we belong to may have been torn down
      // and replaced — disconnect()/Stop aborts the controller and nulls
      // abortRef, and a fresh send (or visibility/online reconnect)
      // installs a NEW controller. The classic case: Stop calls
      // disconnect({clearStreaming:true}) (zeroing justSentAtRef) and
      // ChatView immediately resends via sendMessage → connectToStream
      // with a new controller. If a now-orphaned continuation's fetch
      // resolves here AFTER that, running the 204/terminal-refresh logic
      // below would clobber the freshly-resent turn (e.g. Date.now() -
      // justSentAtRef pushes past the broadcast-registration window and
      // hits the DB-refresh path, wiping the new optimistic message).
      // An aborted fetch normally rejects with AbortError and lands in
      // catch, but the abort can land in the gap between resolution and
      // this line — so bail explicitly whenever we're no longer the
      // active controller.
      if (abortRef.current !== controller) return

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
        wantsReconnectRef.current = false
        setIsStreaming(false)
        setStreamItems([])
        textBufferRef.current = ''
        forceNewTextBlockRef.current = false
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
      wantsReconnectRef.current = true
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

          // First real event on this connection: the catch-up burst is
          // delivering content. It is now safe to clear lastGoodItemsRef
          // because a full replay will rebuild latestItemsRef from scratch.
          if (!catchUpStartedRef.current) {
            catchUpStartedRef.current = true
            lastGoodItemsRef.current = []
          }

          if (SYSTEM_EVENTS.has(event.type)) {
            onSystemEventRef.current?.(event)
            continue
          }

          if (event.type === 'catch_up_done') {
            isCatchUp = false
            continue
          }

          if (event.type === 'text_boundary') {
            flushBuffer()
            forceNewTextBlockRef.current = true
          } else if (event.type === 'text') {
            const content = event.content || ''
            if (isCatchUp) {
              // Catch-up burst: apply immediately, no typewriter.
              setStreamItems(prev => appendTextChunk(prev, content))
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
            // Targets the open tool lifecycle — the last running tool
            // item, or a question card that absorbed its tool block
            // (the post-answer "answers echo" output is swallowed
            // there; see streamReducers.js).
            setStreamItems(prev => attachToolOutput(prev, event.content))
          } else if (event.type === 'tool_end') {
            setStreamItems(prev => closeToolLifecycle(prev))
          } else if (event.type === 'skill_loaded') {
            // Skill observability: stamp the loaded skill's name onto
            // the most recent Skill tool block so ToolBlock renders a
            // chip. Mirrors backend/app/events.py:process_event so the
            // live stream and the persisted transcript agree.
            setStreamItems(prev => {
              const updated = [...prev]
              for (let i = updated.length - 1; i >= 0; i--) {
                if (updated[i].type === 'tool' && updated[i].tool === 'Skill') {
                  updated[i] = { ...updated[i], skill: event.skill }
                  break
                }
              }
              return updated
            })
          } else if (event.type === 'question') {
            const questions = event.questions || []
            if (questions.length > 0 && questions[0]?.question) {
              flushBuffer()
              // Coalesce by stable identity (question id, falling back
              // to text) AND absorb the pending tool item for the same
              // call. The runner publishes tool_start(AskUserQuestion)
              // from the assistant tool_use block before can_use_tool
              // fires this event — same tool use, two wire shapes —
              // so appending here rendered the call twice (a running
              // tool block + the card). upsertQuestionItem replaces
              // the tool item in place (card keeps its position) and
              // carries already-patched answers on re-delivery so a
              // catch-up replay can't re-arm an answered card. See
              // streamReducers.js for the full policy; mirrors
              // backend/app/events.py:process_event identity keying.
              const incoming = { type: 'question', questions }
              if (event.question_id) incoming.question_id = event.question_id
              // Re-arm the replayed event with any answer the user already
              // submitted this turn. After a reconnect wipe upsertQuestionItem
              // has no prior item to carry answers from; this ref does, and it
              // outlived the wipe — so a catch-up replay re-renders the card
              // as ANSWERED instead of reverting it to pending.
              const knownAnswers = answersByQuestionKeyRef.current.get(
                questionKey(incoming)
              )
              if (knownAnswers && !incoming.answers) incoming.answers = knownAnswers
              onLiveQuestionRef.current?.(event.question_id || null)
              setStreamItems(prev => upsertQuestionItem(prev, incoming))
            }
          } else if (event.type === 'answers_applied') {
            // The question was answered (by this tab, another tab, or the
            // in-process answer delivery). Patch the in-flight card to
            // answered and record the answer so a later catch-up replay
            // re-arms it instead of reverting to pending. Without this, an
            // already-connected stream — or a navigate-away-and-back — left
            // the card blank even though the DB block carries the answers,
            // because the persisted answered block is suppressed while a
            // same-id streaming card is still in flight.
            if (event.question_id || event.answers) {
              patchQuestionAnswers(
                event.question_id || null, event.answers || {},
              )
            }
          } else if (event.type === 'error') {
            flushBuffer()
            setStreamItems(prev => {
              // A provider error terminates every open tool lifecycle
              // (running tools flip done; an absorbed question drops
              // its pending-tool marker — no tool_end is coming).
              const updated = closeAllToolLifecycles(prev)
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
            queuedContinuationMessageRef.current = event.message || null
            onQueuedTurnStartingRef.current?.({
              ts: event.ts ?? null,
              message: event.message || null,
            })
          } else if (event.type === 'steered_into_turn') {
            // The backend already put the user message in the transcript;
            // the caller drops the optimistic queued-tray entry and renders
            // it inline as content growth (no send-time spacer/scroll-pin).
            // Flush the typewriter buffer FIRST so the pre-steer assistant
            // text is fully in latestItemsRef before the caller promotes it
            // to its own finished message — without this, the last frame of
            // buffered chars would land in the NEXT (post-steer) turn. The
            // post-steer text then streams in as normal `text` deltas.
            flushBuffer()
            if (isCatchUp) {
              // Replay during the catch-up burst (a mid-A2 reconnect or
              // remount): the DB fetch already returned the sealed pre-steer
              // assistant (A1) AND the steered user row (Q2), so promoting the
              // replayed pre-steer text into a fresh message would DUPLICATE
              // A1, and re-inserting Q2 is redundant. Drop the replayed
              // pre-steer segment from streamItems so the post-steer
              // continuation (A2) accumulates fresh and promotes as its own
              // assistant after Q2. (setStreamItems clears latestItemsRef
              // synchronously too, so a later promote can't resurrect A1.)
              setStreamItems([])
              forceNewTextBlockRef.current = false
            } else {
              onSteeredIntoTurnRef.current?.({
                ts: event.ts ?? null,
                content: event.content || '',
              })
            }
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
            wantsReconnectRef.current = !!continues
            const promotedMessage = queuedContinuationMessageRef.current
            queuedContinuationRef.current = false
            queuedContinuationTsRef.current = null
            queuedContinuationMessageRef.current = null
            // The turn is done — its answers are durable in the promoted
            // message now, so drop the reconnect-survival cache before the
            // next turn (a queued continuation streams on the same hook and
            // must not inherit a stale answer for a re-used question key).
            answersByQuestionKeyRef.current.clear()
            // Delay onStreamEnd by one frame so the React render
            // triggered by setIsStreaming(false) above completes before
            // ChatView promotes streamItems to messages.  latestItemsRef
            // is already up-to-date (synchronous), so this is purely for
            // render consistency (e.g. streaming UI teardown).
            requestAnimationFrame(() => {
              onStreamEndRef.current?.({ continues, promotedTs, promotedMessage })
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
      if (abortRef.current !== controller) return
      abortRef.current = null
      flushBuffer()
      if (!wantsReconnectRef.current) {
        setIsStreaming(false)
        requestAnimationFrame(() => onStreamEndRef.current?.())
        return
      }

      // A bare EOF is not a terminal chat event. Mobile PWAs can see
      // this when the OS freezes or drops the SSE fetch in the
      // background, including before the first event arrives. Keep the
      // turn live so the foreground handler can reattach and replay
      // catch-up instead of hiding the in-progress assistant message.
      setIsStreaming(true)
      if (document.visibilityState === 'visible') {
        setConnectionError('retrying')
        scheduleReconnect(() => connectRef.current?.(true), 300)
      }
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
        // Restore the last-good snapshot so promoteStreamToMessages has
        // something to promote. Every reconnect attempt called
        // connectToStream(resetState=true) which wiped latestItemsRef;
        // if no events arrived on any attempt, latestItemsRef is empty
        // and promoteStreamToMessages's early-return (items.length === 0)
        // would silently discard the user's partial response. Restoring
        // from lastGoodItemsRef here lets the caller promote and display
        // whatever the stream produced before the connection collapsed.
        // On a successful reconnect lastGoodItemsRef was already cleared
        // (catchUpStartedRef fired) so this no-ops there.
        if (lastGoodItemsRef.current.length > 0 && latestItemsRef.current.length === 0) {
          const saved = lastGoodItemsRef.current
          latestItemsRef.current = saved
          _setStreamItems(saved)
        }
        // Retries are exhausted and no reconnect is scheduled, so this is
        // a terminal end of the stream. Signal stream-end like the normal
        // close path above does, otherwise ChatView's `sending` stays true
        // and the thinking dots spin forever.
        requestAnimationFrame(() => onStreamEndRef.current?.())
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
    wantsReconnectRef.current = true
    // Reset state — same reason as the automatic retry above.
    connectRef.current?.(true)
  }, [])

  const sendMessage = useCallback(async (
    text,
    attachments,
    {
      hidden = false,
      queueOnly = false,
      forceSteer = false,
      consumePendingTs = undefined,
      answers = undefined,
      question_id = undefined,
    } = {},
  ) => {
    // Answer submissions (hidden+answers) ride the EXISTING turn —
    // the runner is paused on the AskUserQuestion future and resumes
    // in place. Wiping streamItems here would erase the question card
    // the user just answered, and the post-answer agent output would
    // render as if a fresh turn started (no context for what the user
    // is replying to). Keep streamItems intact for the answer path.
    const isAnswerSubmission = !!answers
    if (!queueOnly && !isAnswerSubmission) {
      wantsReconnectRef.current = true
      justSentAtRef.current = Date.now()
      setStreamItems([])
      textBufferRef.current = ''
      setIsStreaming(true)
      setConnectionError(null)
    }

    let responseData = null
    try {
      const body = { content: text }
      if (hidden) body.hidden = true
      if (forceSteer) body.force_steer = true
      if (consumePendingTs && consumePendingTs.length > 0) {
        body.consume_pending_ts = consumePendingTs
      }
      // AskUserQuestion answers persist atomically with the hidden
      // user message — backend writes them into the question block
      // in the same transaction (see chats_stream.py).
      if (answers) body.answers = answers
      if (question_id) body.question_id = question_id
      if (attachments && attachments.length > 0) {
        body.attachments = attachments
      }
      try { body.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone } catch {}
      // Viewport height: prefer the CURRENT visual viewport so the
      // agent's screenshots match the partner's framing right now.
      // Fall back to the max-ever-observed only when current looks
      // keyboard-poisoned (significantly smaller than max — a soft
      // keyboard subtracts ~250-300px on phones, so a >100px gap is
      // the heuristic). Pure max-tracking was wrong: if the user
      // opens the chat with the URL bar collapsed (taller viewport)
      // then re-shows it, the stale max yielded a too-tall screenshot.
      // Pure current was also wrong: POSTs that land before the
      // keyboard fully dismisses get the shrunken height. The blend
      // gets the right value in both cases.
      const cur = (typeof window.visualViewport !== 'undefined' && window.visualViewport)
        ? window.visualViewport.height
        : window.innerHeight
      const maxH = maxInnerHeightRef.current || 0
      const keyboardLikely = maxH > 0 && cur < maxH - 100
      body.viewport = {
        width: window.innerWidth,
        height: keyboardLikely ? maxH : cur,
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
      responseData = data
      // Trust the backend's actual status, not the frontend's queueOnly
      // hint. The frontend's `sending` flag can be stale (turn finished
      // between the doSend check and the POST landing), so a request
      // sent with queueOnly:true can come back as "started". Always
      // connect to the stream when the backend says it started.
      if (data.status === 'queued' && !data.started) return data
      if (data.status === 'steered') return data
      if (data.status === 'not_steered') return data
      // AskUserQuestion answer was delivered in-process to the parked
      // future — the runner resumes the EXISTING turn with the answer.
      // No new stream connection needed; the existing SSE keeps
      // streaming whatever the runner emits next. Returning here
      // prevents a redundant reconnect that would close the live
      // stream and replay the full catch-up burst.
      if (data.status === 'answer_delivered') return data
      // Started: ensure streaming state is set even if the caller
      // passed queueOnly:true expecting it would be queued.
      if (queueOnly || data.status === 'queued') {
        wantsReconnectRef.current = true
        justSentAtRef.current = Date.now()
        setStreamItems([])
        textBufferRef.current = ''
        forceNewTextBlockRef.current = false
        setIsStreaming(true)
        setConnectionError(null)
      }
    } catch (err) {
      // Always reset streaming state on POST failure. The earlier
      // `if (!queueOnly)` guard left the UI stuck on "thinking" dots
      // when a queueOnly send raced the server's `status: 'started'`
      // branch and then the POST itself failed mid-flight.
      wantsReconnectRef.current = false
      setIsStreaming(false)
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
    return responseData || { status: 'started' }
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
      if (!wantsReconnectRef.current && !isStreamingRef.current) return
      if (abortRef.current) {
        abortRef.current.abort()
        abortRef.current = null
      }
      connectRef.current?.(true)
    }

    function onOnline() {
      if (!wantsReconnectRef.current && !isStreamingRef.current && !connectionErrorRef.current) return
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

  // Patch the `answers` field on the matching question item in streamItems.
  // Used by doSendSilent's optimistic update when the question is still live
  // in streamItems (not yet promoted to messages) — without this, the
  // answered state only lands on messages[-1] (which may be the user message,
  // not the assistant), so the card never visually transitions to answered.
  function patchQuestionAnswers(questionId, answers) {
    const key = questionId ? `question_id:${questionId}` : null
    // Record the answer keyed by stable identity BEFORE touching streamItems,
    // so a later reconnect's catch-up replay (which wipes streamItems first)
    // can re-arm the replayed question event instead of reverting the card to
    // pending. When the questionId is known we record under that key directly;
    // an id-less (text-keyed) question is recorded from the matched item below.
    if (key) answersByQuestionKeyRef.current.set(key, answers)
    setStreamItems(prev => {
      return prev.map(it => {
        if (it.type !== 'question') return it
        // When we have a questionId, match by id; otherwise patch the
        // first question item (mirrors the single-question-per-turn norm).
        const itKey = questionKey(it)
        if (key ? itKey === key : true) {
          if (!key) answersByQuestionKeyRef.current.set(itKey, answers)
          return { ...it, answers }
        }
        return it
      })
    })
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
    patchQuestionAnswers,
  }
}
