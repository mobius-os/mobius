/**
 * Subtle reconnection indicator shown when the SSE connection is lost.
 */
export default function ConnectionStatus({ error, onRetry }) {
  if (!error) return null

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
          <button className="connection-status__retry" onClick={onRetry}>
            Retry
          </button>
        </>
      )}
    </div>
  )
}
