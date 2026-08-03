const MEDIA_CONTROLS = new Set(['play', 'pause', 'stop'])

function sameSession(owner, appId, sessionId) {
  return owner
    && String(owner.appId) === String(appId)
    && owner.sessionId === sessionId
}

/**
 * Own the shell's single app playback lease. Apps retain the media graph and
 * confirm every state transition; the shell only forwards controls and paints
 * the latest confirmed metadata.
 */
export function createMediaSessionOwner(onChange) {
  let owner = null

  return {
    receive(appId, event, sendControl) {
      const matches = sameSession(owner, appId, event.sessionId)
      if (event.event === 'close') {
        if (!matches) return false
        owner = null
        onChange(null)
        return true
      }
      if (event.event === 'update' && !matches) return false
      if (event.event === 'open' && owner && !matches) {
        owner.sendControl?.('stop')
      }
      owner = { appId, sessionId: event.sessionId, sendControl }
      onChange({
        appId,
        sessionId: event.sessionId,
        title: event.title,
        playbackState: event.playbackState,
      })
      return true
    },

    control(action) {
      if (!owner || !MEDIA_CONTROLS.has(action)) return false
      // A stop request is not completion. Keep the lease visible until the app
      // closes it, so a dead/stale frame cannot leave audible media unowned.
      return owner.sendControl?.(action) === true
    },
  }
}
