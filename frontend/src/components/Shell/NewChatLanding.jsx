/**
 * Passive home for a genuinely empty Standard slot.
 *
 * Interactive New Chat actions never render here: Shell mounts the canonical
 * ChatView immediately on the client-minted final id. Keeping this component
 * passive prevents a second composer from owning draft, files, focus, model
 * settings, or first-send state.
 */
export default function NewChatLanding({ failure = null, onRetry }) {
  return (
    <div className="chat chat--empty">
      <div className="chat__empty-wrap">
        <div className="chat__empty">
          <img className="chat__empty-glyph" src="/moebius.png" alt="" width="76" height="76" />
          <p className="chat__empty-title">What&apos;s on your mind?</p>
          {failure && (
            <>
              <p className="chat__empty-sub" role="status">
                {failure === 'offline'
                  ? 'You’re offline — a new chat needs the network.'
                  : 'Couldn’t start a new chat — please try again.'}
              </p>
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
