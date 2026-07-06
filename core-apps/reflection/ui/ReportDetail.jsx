import { useCallback, useEffect, useRef, useState } from 'react'
import { CHAT_PANE_MIN_PX } from '../constants.js'
import {
  clampChatRatio,
  extractReportQuestions,
  hardenReportHtml,
  relativeLabel,
  reportThemeStyle,
  subLabel,
} from '../domain.js'
import {
  chatOpenKey,
  chatRatioKey,
  readChatOpen,
  readChatRatio,
} from '../storage.js'
import { ChatBubbleIcon } from './ChatBubbleIcon.jsx'
import { ChatPanel } from './ChatPanel.jsx'
import { ReportQuestions } from './ReportQuestions.jsx'

// Report detail — the brief read, with an app-scoped chat one tap away.
//
// The brief is the static HTML the agent authored: rendered in a sandboxed
// srcDoc iframe. Sandbox: allow-scripts WITHOUT allow-same-origin — the
// iframe has a null origin so its scripts cannot access the parent's DOM,
// localStorage, or owner JWT (the security risk of allow-same-origin+scripts).
// hardenReportHtml injects a tiny height-reporter script that postMessages
// the content height to the parent. The parent sizes the iframe from those
// messages so the brief reads as one scrolled column. The chat icon next to
// the report name toggles a 50/50 split (a draggable divider, the read on top
// and the embedded ChatView below) hosting the app-scoped chat (see ChatPanel)
// — durable and not tied to any one brief.
// ---------------------------------------------------------------------------

export function ReportDetail({ dateStr, storage, online, onBack, appId, token }) {
  const [state, setState] = useState({ phase: 'loading', html: '' })
  // The agent's in-report questions, extracted from the RAW brief HTML and
  // rendered as native tap cards below the iframe. The carrier is stripped
  // from the HTML before hardenReportHtml so it never reaches the iframe.
  const [questions, setQuestions] = useState([])
  const [chatOpen, setChatOpen] = useState(() => readChatOpen(appId))
  const [chatRatio, setChatRatio] = useState(() => readChatRatio(appId))
  const [briefHeight, setBriefHeight] = useState(360)
  const [reloadKey, setReloadKey] = useState(0)
  const iframeRef = useRef(null)
  // The detail body — the resize math measures its height to convert a pointer
  // drag into a 0..1 ratio.
  const bodyRef = useRef(null)

  // Persist chat open + split ratio per app (mirrors app-latex).
  useEffect(() => {
    if (typeof localStorage === 'undefined') return
    try { localStorage.setItem(chatOpenKey(appId), String(chatOpen)) } catch {}
  }, [appId, chatOpen])
  useEffect(() => {
    if (typeof localStorage === 'undefined') return
    try { localStorage.setItem(chatRatioKey(appId), String(chatRatio)) } catch {}
  }, [appId, chatRatio])

  // Open always spawns a fresh 50/50 split, regardless of where a prior drag
  // left the divider (owner spec, app-latex parity).
  const toggleChat = useCallback(() => {
    setChatOpen((open) => {
      if (!open) setChatRatio(0.5)
      return !open
    })
  }, [])

  // Drag the divider: convert vertical pointer movement into a chat ratio,
  // px-bounded so the chat collapses to exactly the composer pill and no
  // smaller, and the report keeps at least one pill visible. Ported from
  // app-latex (same pointer-capture teardown for an interrupted drag —
  // pointercancel / lostpointercapture, not just pointerup).
  const beginChatResize = useCallback((event) => {
    event.preventDefault()
    const body = bodyRef.current
    if (!body) return
    const total = body.getBoundingClientRect().height
    if (!total) return
    const startY = event.clientY
    const startRatioPx = total * chatRatio
    const divider = event.currentTarget
    const pointerId = event.pointerId
    divider.setPointerCapture?.(pointerId)
    const onMove = (moveEvent) => {
      const desiredPx = startRatioPx + startY - moveEvent.clientY
      setChatRatio(clampChatRatio(desiredPx, total, CHAT_PANE_MIN_PX))
    }
    const endDrag = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', endDrag)
      window.removeEventListener('pointercancel', endDrag)
      divider.removeEventListener('lostpointercapture', endDrag)
      try { divider.releasePointerCapture?.(pointerId) } catch {}
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', endDrag)
    window.addEventListener('pointercancel', endDrag)
    divider.addEventListener('lostpointercapture', endDrag)
  }, [chatRatio])

  // Keyboard resize on the focused divider: Arrows step ~6%, Home collapses the
  // chat to the pill, End leaves one pill of report — all clamped by the same
  // floors as the drag path.
  const handleResizeKey = useCallback((event) => {
    const total = bodyRef.current?.getBoundingClientRect().height || 0
    if (!total) return
    const step = total * 0.06
    if (event.key === 'ArrowUp') {
      event.preventDefault()
      setChatRatio((r) => clampChatRatio(r * total + step, total, CHAT_PANE_MIN_PX))
    } else if (event.key === 'ArrowDown') {
      event.preventDefault()
      setChatRatio((r) => clampChatRatio(r * total - step, total, CHAT_PANE_MIN_PX))
    } else if (event.key === 'Home') {
      event.preventDefault()
      setChatRatio(clampChatRatio(0, total, CHAT_PANE_MIN_PX))
    } else if (event.key === 'End') {
      event.preventDefault()
      setChatRatio(clampChatRatio(total, total, CHAT_PANE_MIN_PX))
    }
  }, [])

  // Coming back online after a failed (offline) brief load should retry the
  // body rather than stranding the reader on the offline error until they
  // navigate away and back. We bump reloadKey on the false→true transition;
  // the load effect below depends on it. (A successful brief is unaffected —
  // re-running the fetch with cache:'no-store' just re-reads the same body.)
  const wasOnline = useRef(online)
  useEffect(() => {
    if (online && !wasOnline.current) setReloadKey((k) => k + 1)
    wasOnline.current = online
  }, [online])

  // Load the brief body.
  useEffect(() => {
    let cancelled = false
    setState({ phase: 'loading', html: '' })
    setQuestions([])
    setBriefHeight(360)
    ;(async () => {
      const res = await storage.getReportHtml(`${dateStr}.html`)
      if (cancelled) return
      if (res.data != null) {
        // Extract the question carrier from the RAW HTML BEFORE hardening: the
        // inert carrier <script> would otherwise ride into the sandboxed iframe
        // and the visible <section>/<h2> shell would render as raw text in the
        // brief (and the partner can't answer there). Strip it, harden the
        // remainder, render the questions natively below.
        const { html: cleaned, questions: qs } = extractReportQuestions(res.data)
        setQuestions(qs)
        setState({ phase: 'ready', html: hardenReportHtml(cleaned, reportThemeStyle()) })
      }
      else if (res.notFound) setState({ phase: 'missing', html: '' })
      else {
        setState({ phase: 'error', html: '' })
        window.mobius?.signal?.('error', { message: 'brief load failed for ' + dateStr + ' (HTTP ' + res.error + ')' })
      }
    })()
    return () => { cancelled = true }
  }, [dateStr, storage, reloadKey])

  // Size the brief iframe from postMessage events sent by the injected
  // height-reporter script (see hardenReportHtml + REPORT_HEIGHT_SCRIPT).
  // The iframe runs with allow-scripts but WITHOUT allow-same-origin, so
  // contentDocument is NOT readable from the parent — we receive height
  // passively via postMessage instead.
  useEffect(() => {
    const onMessage = (ev) => {
      if (!ev.data || ev.data.type !== 'reflection:brief-height') return
      // Only trust OUR brief iframe: the sandboxed frame has a null origin,
      // so ev.origin can't identify it — ev.source against the iframe's
      // contentWindow is the only way to reject spoofed height messages
      // from other windows.
      if (ev.source !== iframeRef.current?.contentWindow) return
      const h = Number(ev.data.height)
      if (Number.isFinite(h) && h > 0) {
        // The reported height is applied as-is (no buffer): the reporter
        // sends Math.ceil of an exact content metric, and re-applying a
        // buffer per emit would creep the height upward. Clamp to a sane
        // ceiling: a malformed/runaway report (broken layout, a script in
        // an infinite-growth loop) could report an enormous height and
        // grow the outer column unboundedly. 16000px is well past any real
        // one-page brief; beyond it the iframe scrolls its own overflow
        // rather than the parent column stretching forever.
        setBriefHeight(Math.min(Math.max(h, 200), 16000))
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])

  const onIframeLoad = useCallback(() => {
    // The height reporter inside the iframe fires on DOMContentLoaded and
    // on ResizeObserver changes. Nothing to do here from the parent side,
    // but we keep the onLoad prop in case subclasses need it later.
  }, [])

  return (
    <div className="rf-detail rf-rise">
      <div className="rf-detail-bar">
        <button
          className="rf-back-btn rf-pressable"
          onClick={onBack} aria-label="Back to reports"
        >
          <span aria-hidden="true" className="rf-back-glyph">‹</span> Briefs
        </button>
        <div className="rf-detail-title">
          <span className="rf-detail-title-main">{relativeLabel(dateStr)}’s brief</span>
          <span className="rf-detail-title-sub">{subLabel(dateStr)}</span>
        </div>
        <button
          type="button"
          className="rf-chat-toggle rf-pressable"
          aria-label="Chat about your briefs"
          aria-pressed={chatOpen}
          title="Chat"
          onClick={() => {
            // Engagement signal on the closed→open edge only (once per open),
            // replacing the signal the removed FeedbackLauncher used to emit.
            if (!chatOpen) window.mobius?.signal?.('feedback_given', { date: dateStr, signal: 'chat' })
            toggleChat()
          }}
        >
          <ChatBubbleIcon size={20} />
        </button>
      </div>

      {/* The detail body. When the chat is open it becomes a vertical split:
          the brief read scrolls in the top pane, a draggable divider sits in
          the middle, and the app-scoped chat fills the bottom --chat-ratio
          share (the same layout app-latex / app-webstudio use). When closed it
          is just the scrolling read. */}
      <div
        ref={bodyRef}
        className="rf-detail-body"
        style={chatOpen ? { '--chat-ratio': chatRatio, '--chat-pane-min': `${CHAT_PANE_MIN_PX}px` } : undefined}
      >
        <div className="rf-split-body rf-scroll">
          {state.phase === 'loading' && (
            <div className="rf-brief-loading">
              <span className="rf-spinner" aria-hidden="true" />
              <span>Opening your brief…</span>
            </div>
          )}

          {state.phase === 'missing' && (
            <div className="rf-empty is-compact">
              This brief is no longer available.
            </div>
          )}

          {state.phase === 'error' && (
            <div className="rf-empty is-compact">
              {online
                ? 'This brief could not be loaded. Try opening it again in a moment.'
                : 'You’re offline — open this brief again once you’re back online.'}
            </div>
          )}

          {state.phase === 'ready' && (
            <>
              <div className="rf-brief-panel">
                <iframe
                  ref={iframeRef}
                  className="rf-brief-iframe"
                  style={{ height: `${briefHeight}px` }}
                  title={`Morning brief for ${dateStr}`}
                  srcDoc={state.html}
                  onLoad={onIframeLoad}
                  // allow-scripts lets the injected height-reporter run.
                  // allow-same-origin is intentionally absent: without it the
                  // iframe gets a null origin, so its scripts cannot reach the
                  // parent's DOM, localStorage, or owner JWT regardless of what
                  // the brief HTML contains. allow-popups lets the agent include
                  // external links that open in a new tab.
                  sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
                />
              </div>

              {/* In-brief question cards render inline below the read. The
                  carrier was extracted from the raw HTML and stripped before
                  srcDoc, so these taps are the interactive surface. Answers
                  persist to question-answers/<date>.json for the NEXT run — no
                  live agent waits (a background AskUserQuestion would park a
                  future a server reset orphans). The card owns its own durable
                  write so it can await the result, flip to "Saved" on a durable
                  outcome (synced or queued), and re-seed the answered state from
                  storage when the brief reopens. */}
              {questions.length > 0 && (
                <ReportQuestions
                  questions={questions}
                  storage={storage}
                  dateStr={dateStr}
                  appId={appId}
                  token={token}
                />
              )}
            </>
          )}
        </div>

        {chatOpen && (
          <>
            <div
              className="rf-chat-divider"
              role="separator"
              aria-label="Resize brief and chat areas"
              aria-orientation="horizontal"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(chatRatio * 100)}
              tabIndex={0}
              onPointerDown={beginChatResize}
              onKeyDown={handleResizeKey}
            >
              <span className="rf-chat-divider-bar" aria-hidden="true" />
            </div>
            <ChatPanel getContext={() => ({ app: 'reflection', report_date: dateStr })} />
          </>
        )}
      </div>
    </div>
  )
}
