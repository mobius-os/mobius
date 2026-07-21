import { BASE } from '../../api/client.js'

/**
 * The first-class New Chat landing painted for a null single-screen slot
 * (round 4 item 3). A null slot is a DEFINITE New Chat destination now — never the
 * freshest transcript — so the honest beat destination is this cheap, always-available
 * surface. It shares ChatView's empty-treatment visuals (the same .chat__empty-wrap /
 * .chat__empty glyph + title) so the swap to a real empty ChatView, once the row
 * materializes, is visually seamless. It also doubles as the phase-2 world-reveal
 * underlay while the beat runs.
 *
 * It is deliberately NOT a live composer: drafts, attachments, provider state, and the
 * send pipeline are all chat-ID-bound, so the row is materialized AFTER the descriptor
 * idles and this landing is replaced by the real empty ChatView. If creation is
 * offline/failed the retry affordance keeps the surface honest — never a blank <main>,
 * never chats[0].
 */
export default function NewChatLanding({ offline = false, onRetry }) {
  // The .chat.chat--empty wrapper reuses ChatView's exact empty layout (opaque --bg
  // fill + centered cluster with the fixed bottom reservation), so the swap to a real
  // empty ChatView is seamless.
  return (
    <div className="chat chat--empty">
      <div className="chat__empty-wrap">
        <div className="chat__empty">
          <img className="chat__empty-glyph" src={`${BASE}/moebius.png`} alt="" width="120" height="120" />
          <p className="chat__empty-title">What&apos;s on your mind?</p>
          {offline && (
            <>
              <p className="chat__empty-sub">You&apos;re offline — a new chat needs the network.</p>
              {onRetry && (
                <button type="button" className="chat__empty-action" onClick={onRetry}>
                  Retry
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
