import { useEffect, useRef, useState } from 'react'

// ---------------------------------------------------------------------------
// App-scoped chat, presented as the bottom half of a 50/50 split — the same
// pattern app-latex / app-webstudio use (a draggable divider between the report
// read above and the chat below), so the chat reads the same across apps.
// `window.mobius.chat` mounts the real ChatView (composer + live SSE + tappable
// AskUserQuestion cards) inside a nested same-origin iframe that runs in the
// SHELL origin — so it carries the owner JWT and can read/post chats (the app
// token alone is 403'd on /api/chats; this is the supported path). The runtime
// creates the chat once and persists its id under `chat_id.json`, reusing it on
// later mounts — so the conversation about your briefs is durable and
// app-scoped.
//
// Mounted only while the split is open (rendered by ReportDetail under
// `chatOpen`); closing the panel unmounts it and the cleanup destroys the
// handle — exactly app-latex's lifecycle. `getContext` is read through a ref
// updated by its own effect, so its identity changing (it closes over the
// report date) never re-fires the mount effect and remounts the iframe. The
// runtime publishes no composer-height var, so the panel floors its height at
// the standard composer pill (see CHAT_PANE_MIN_PX) — the embed's input is
// never clipped.
// ---------------------------------------------------------------------------

export function ChatPanel({ getContext }) {
  const mountRef = useRef(null)
  const [phase, setPhase] = useState('mounting') // mounting | live | unavailable
  // getContext is read through a ref so its identity changing (it closes over
  // the report date) never re-fires the mount effect and remounts the iframe.
  const getContextRef = useRef(getContext)
  useEffect(() => { getContextRef.current = getContext }, [getContext])

  useEffect(() => {
    const mount = mountRef.current
    if (!mount) return undefined
    if (!window.mobius || typeof window.mobius.chat !== 'function') {
      // Running outside the shell embed (e.g. standalone) — no chat bridge.
      setPhase('unavailable')
      return undefined
    }
    let handle = null
    let disposed = false
    setPhase('mounting')
    Promise.resolve(window.mobius.chat({
      mount,
      persist: 'chat_id.json',
      title: 'Reflection',
      picker: true,
      getContext: () => {
        const fn = getContextRef.current
        return fn ? fn() : null
      },
    }))
      .then((h) => {
        if (disposed) { try { h && h.destroy && h.destroy() } catch {} return }
        handle = h
        setPhase('live')
      })
      .catch(() => { if (!disposed) setPhase('unavailable') })
    return () => {
      disposed = true
      try { handle && handle.destroy && handle.destroy() } catch {}
      // Belt-and-suspenders: the runtime appends one iframe to `mount`; clear
      // any leftover node so we never leak or stack the nested embed.
      if (mount) { try { mount.replaceChildren() } catch {} }
    }
  }, [])

  return (
    <section className="rf-chat-panel" aria-label="Chat about your briefs">
      {phase === 'unavailable' ? (
        <div className="rf-no-chat-note">
          <span aria-hidden="true" className="rf-no-chat-glyph">💬</span>
          <span>
            The chat about your briefs isn’t available here. Open it from your
            chat list to reply.
          </span>
        </div>
      ) : (
        <>
          <div className="rf-chat-hint">
            Share feedback on today’s brief — what landed, what didn’t. Your notes steer tomorrow’s run.
          </div>
          {phase === 'mounting' && (
            <div className="rf-chat-resolving">
              <span className="rf-spinner rf-spinner-sm" aria-hidden="true" />
              Opening the conversation…
            </div>
          )}
          <div ref={mountRef} className="rf-chat-embed" style={{ display: phase === 'live' ? 'block' : 'none' }} />
        </>
      )}
    </section>
  )
}
