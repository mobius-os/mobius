// Immersive mode — the `moebius:immersive` postMessage protocol's shell-side
// state (.pm/128). An app (games, primarily) posts
// {type:'moebius:immersive', value, appId} to ask the shell to hide its top
// bar so the canvas fills the entire viewport, including under the phone
// notch. This module is the pure state core; Shell.jsx owns the reducer
// instance and AppCanvas.jsx feeds it verified messages.
//
// Trust model: the appId in a dispatched action comes from the AppCanvas
// that VERIFIED event.source === its own iframe's contentWindow — never from
// the message payload — so a frame can only toggle immersive for itself.
//
// The state is the id of the app that currently HOLDS an immersive request,
// or null. Holding a request is not the same as immersive being APPLIED:
// application additionally requires that app to be the active canvas (see
// isImmersiveActive).
//
// Immersive intent is deliberately SESSIONAL, not sticky navigation state.
// Leaving an app releases its request, and returning does not resurrect a
// previously recorded `true`: the app must make a fresh request while it is
// visible. This makes the owner's shell-level Exit action durable across an
// app switch and prevents an eager game from repeatedly taking the workspace
// over merely because its cached iframe stayed mounted. A live frame promotion
// is the one replay boundary; AppCanvas owns that distinction below.

export function immersiveReducer(immersiveAppId, action) {
  switch (action.type) {
    case 'request':
      // value:true grants the requesting app the immersive slot (last
      // writer wins); value:false releases it ONLY if that app holds it —
      // a hidden sibling's cleanup must not strip another app's request.
      // AppCanvas dispatches value:false both for an app's own
      // cleanup-post and on iframe teardown (unmount / eviction / version
      // remount), so "app switch or unmount always restores" holds even
      // though tearing down an iframe never runs the app's own effects.
      if (action.value) return action.appId
      return sameApp(immersiveAppId, action.appId) ? null : immersiveAppId
    case 'exit':
      // The shell's floating exit button — the user always wins. The app
      // is NOT consulted; it only re-enters immersive by posting again
      // (which a mounted app won't do until it remounts).
      return null
    default:
      return immersiveAppId
  }
}

// Immersive is applied only while the requesting app is what the user is
// actually looking at. Everything else — chat, settings, another app —
// keeps normal chrome, which is what makes app-switch restoration
// automatic: no event needs to fire, the condition just stops holding.
export function isImmersiveActive(immersiveAppId, activeView, activeAppId) {
  return immersiveAppId != null
    && activeView === 'canvas'
    && sameApp(immersiveAppId, activeAppId)
}

/**
 * Decide whether AppCanvas should drive Shell from a lifecycle transition.
 *
 * Real-time frame messages are forwarded directly and do not pass through
 * this helper. Lifecycle replay is narrower:
 *   - becoming inactive releases the holder;
 *   - returning to the same cached frame does nothing (no sticky re-entry);
 *   - promoting a newly loaded frame while continuously active replays that
 *     new frame's recorded intent, so an in-place app update stays seamless.
 *
 * `null` means no callback. A boolean is the value to send to Shell.
 */
export function immersiveLifecycleValue(previous, current, recordedIntent) {
  if (!current?.appId) return null
  if (!current.active) return false

  const sameCanvas = previous?.appId != null
    && String(previous.appId) === String(current.appId)
  const promotedWhileActive = sameCanvas
    && previous.active === true
    && previous.liveVersion !== current.liveVersion

  if (!promotedWhileActive) return null
  return recordedIntent === true
}

// App ids are numeric from /api/apps but arrive as strings on some paths
// (deep links, dataset reads); compare by value, not identity.
function sameApp(a, b) {
  return a != null && b != null && String(a) === String(b)
}
