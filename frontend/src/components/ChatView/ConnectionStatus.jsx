/**
 * Subtle reconnection indicator shown when the SSE connection is lost,
 * plus a quieter note while a wake/online reattach is in flight.
 */
export default function ConnectionStatus({ error, reconnecting, onRetry }) {
  if (!error) {
    // `reconnecting` is the healthy sleep/wake reattach window (see
    // useStreamConnection's armReconnectingNote): the stream is being
    // replaced, not failing, so it renders as a quiet note without the
    // error bar or a Retry affordance. Error states below win the slot —
    // 'retrying' already announces its own reconnect, and 'disconnected'
    // needs the Retry button front and center.
    if (!reconnecting) return null
    return (
      <div
        className="connection-status connection-status--reattach"
        role="status"
        aria-live="polite"
      >
        <span className="connection-status__text">Reconnecting…</span>
      </div>
    )
  }

  // Announce a dropped stream to assistive tech: 'alert' (assertive) for the
  // terminal "connection lost" so a screen-reader user hears it immediately
  // and can find Retry; 'status' (polite) for the transient reconnecting
  // state so it doesn't interrupt.
  const isLost = error !== 'retrying'
  return (
    <div
      className="connection-status"
      role={isLost ? 'alert' : 'status'}
      aria-live={isLost ? 'assertive' : 'polite'}
    >
      {error === 'retrying' ? (
        <span className="connection-status__text">Reconnecting...</span>
      ) : (
        <>
          <span className="connection-status__text">Connection lost</span>
          <button
            type="button"
            className="connection-status__retry"
            onClick={onRetry}
          >
            Retry
          </button>
        </>
      )}
    </div>
  )
}
