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
// The state is the app's complete request ({ appId, mode }) or null. Keeping
// ownership and presentation together prevents a mode from leaking from a
// previous holder. Holding a request is not the same as immersive being
// APPLIED: application additionally requires that app to be the active canvas
// (see isImmersiveActive).
//
// Immersive intent is deliberately SESSIONAL, not sticky navigation state.
// Leaving an app releases its request, and returning does not resurrect a
// previously recorded `true`: the app must make a fresh request while it is
// visible. This makes the owner's shell-level Exit action durable across an
// app switch and prevents an eager game from repeatedly taking the workspace
// over merely because its cached iframe stayed mounted. A live frame promotion
// is the one replay boundary; AppCanvas owns that distinction below.

export function immersiveReducer(request, action) {
  switch (action.type) {
    case 'request':
      // value:true grants the requesting app the immersive slot (last
      // writer wins); value:false releases it ONLY if that app holds it —
      // a hidden sibling's cleanup must not strip another app's request.
      // AppCanvas dispatches value:false both for an app's own
      // cleanup-post and on iframe teardown (unmount / eviction / version
      // remount), so "app switch or unmount always restores" holds even
      // though tearing down an iframe never runs the app's own effects.
      if (action.value) {
        const next = {
          appId: action.appId,
          mode: action.mode === 'bar' ? 'bar' : 'full',
        }
        return sameApp(request?.appId, next.appId) && request.mode === next.mode
          ? request
          : next
      }
      return sameApp(request?.appId, action.appId) ? null : request
    case 'exit':
      // The shell's floating exit button — the user always wins. The app
      // is NOT consulted; it only re-enters immersive by posting again
      // (which a mounted app won't do until it remounts).
      return null
    default:
      return request
  }
}

// Immersive is applied only while the requesting app is what the user is
// actually looking at. Everything else — chat, settings, another app —
// keeps normal chrome, which is what makes app-switch restoration
// automatic: no event needs to fire, the condition just stops holding.
//
// Side-by-side panes are the owner's choice to look at several things at once,
// so an app sharing the screen keeps its pane; the request is held, not applied,
// and takes effect once that pane has the screen (maximized or single view).
// Applying it would also resize the requester, and an app that decides by its
// own width would then release and re-request in a loop.
export function isImmersiveActive(request, activeView, activeAppId, { sharesScreen = false } = {}) {
  return request?.appId != null
    && !sharesScreen
    && activeView === 'canvas'
    && sameApp(request.appId, activeAppId)
}

/**
 * Decide whether AppCanvas should drive Shell from a lifecycle transition.
 *
 * Real-time frame messages are forwarded directly and do not pass through
 * this helper. Lifecycle replay is narrower:
 *   - becoming inactive releases the holder;
 *   - returning to the same cached frame does not resurrect a request made
 *     on an earlier visit (no sticky re-entry);
 *   - a request the frame made on THIS return, while it was already shown but
 *     before it became the active canvas (`requestedOnReturn`), is honoured on
 *     activation: the shell tells a frame it is visible before it marks the
 *     pane active, so an app that asks as soon as it reappears would otherwise
 *     lose that fresh request to ordering alone;
 *   - promoting a newly loaded frame while continuously active replays that
 *     new frame's recorded intent, so an in-place app update stays seamless.
 *
 * `null` means no callback. A boolean is the value to send to Shell.
 */
export function immersiveLifecycleValue(previous, current, recordedIntent, requestedOnReturn = false) {
  if (!current?.appId) return null
  if (!current.active) return false

  const sameCanvas = previous?.appId != null
    && String(previous.appId) === String(current.appId)
  if (!sameCanvas) return null

  if (previous.active !== true) {
    return requestedOnReturn && recordedIntent === true ? true : null
  }
  if (previous.liveVersion === current.liveVersion) return null
  return recordedIntent === true
}

/**
 * One canvas's immersive bookkeeping between frame messages and lifecycle
 * transitions. AppCanvas reports what happened; this decides what reaches
 * Shell, so the visit-bounded rules stay in one testable place.
 *
 * A live frame that posts while shown but not active leaves a pending
 * request. It lives for exactly one showing: the next lifecycle transition
 * consumes it, and the frame being hidden discards it. That keeps a post made
 * during Shell's chat handoff (frame still painted after the pane went
 * inactive) from replaying on a later return. An unfocused split pane is
 * shown but inactive too; its request is honoured when the pane is focused,
 * because the user could see the app when it asked and AppCanvas cannot tell
 * that apart from a return. A later `false` post cancels it.
 */
export function createImmersiveLifecycle() {
  let previous = null
  let pendingVersion = null
  return {
    // A frame's moebius:immersive post. Returns whether to forward it to
    // Shell now (only the live frame of the active canvas drives chrome).
    frameRequested({ version, liveVersion, active, frameVisible }) {
      if (version !== liveVersion) return false
      if (active) return true
      if (frameVisible) pendingVersion = version
      return false
    },
    frameHidden() {
      pendingVersion = null
    },
    // `current` is { appId, liveVersion, active }; returns immersiveLifecycleValue's result.
    transition(current, recordedIntent) {
      const value = immersiveLifecycleValue(
        previous,
        current,
        recordedIntent,
        pendingVersion != null && pendingVersion === current.liveVersion,
      )
      pendingVersion = null
      previous = current
      return value
    },
  }
}

// App ids are numeric from /api/apps but arrive as strings on some paths
// (deep links, dataset reads); compare by value, not identity.
function sameApp(a, b) {
  return a != null && b != null && String(a) === String(b)
}
