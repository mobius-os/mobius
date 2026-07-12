/**
 * Registry of HIDDEN incoming preview frames' contentWindows.
 *
 * During AppCanvas's double-buffered version swap (lib/previewSwapState.js) a
 * not-yet-promoted frame runs the app's new module invisibly. Shell-level
 * postMessage handlers deliberately do NOT verify e.source (single-owner trust
 * model — see Shell.jsx onMessage), but one message type is actively harmful
 * from a hidden frame: `moebius:app-error`. Failed swaps are usually BROKEN
 * builds — that is precisely why they fail — and a hidden broken frame
 * planting a crash-report draft (or yanking the view to a chat) while the
 * owner's visible preview still works reads as a phantom crash. Shell's
 * app-error branch consults this registry and ignores errors whose e.source is
 * an incoming frame; the failure is already handled by the swap state machine
 * (the old working frame stays live, the next rebuild retries).
 *
 * A WeakSet rather than a Map with explicit cleanup: a DISCARDED incoming
 * frame's contentWindow cannot be unregistered at unmount (iframe.contentWindow
 * is null once detached), and its queued messages SHOULD stay ignored — the
 * frame was rejected. Promotion is the only transition that must actively
 * clear membership (the frame's messages are the visible app's from then on);
 * AppCanvas does that the moment a frame becomes live. Everything else ages
 * out via GC.
 */
const incoming = new WeakSet()

/** Mark a window as belonging to a hidden, not-yet-promoted incoming frame. */
export function markIncomingFrameWindow(win) {
  if (win) incoming.add(win)
}

/** Clear a window on promotion — it now speaks for the visible app. */
export function clearIncomingFrameWindow(win) {
  if (win) incoming.delete(win)
}

/** Is this message source a hidden incoming frame (current or discarded)? */
export function isIncomingFrameWindow(win) {
  return win != null && incoming.has(win)
}
