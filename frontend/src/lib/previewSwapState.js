/**
 * Double-buffered preview version swap — the pure state machine.
 *
 * Pulled out of AppCanvas.jsx (like lib/appToken.js) so the swap logic is
 * unit-testable without a browser or React.
 *
 * WHY THIS EXISTS
 * The agent recompiles a mini-app while the owner watches it in the iframe
 * preview. Every successful recompile bumps app.updated_at, which the shell
 * turns into a new `version`. The old design fed `${appId}-${version}` as the
 * iframe React key, so a version bump REMOUNTED the one iframe: the running app
 * blanked to a full-frame spinner and lost all in-app state (form text, scroll,
 * current view) on EVERY ~1s incremental save. The design invariant "a rebuild
 * is never disruptive" held for the shell but not for the preview, which is the
 * artifact actually being built.
 *
 * THE MODEL
 * Keep the CURRENT (live) frame visible and interactive; when a newer version
 * arrives, mount it in a HIDDEN incoming frame alongside the live one; when the
 * incoming frame reports it has rendered (`moebius:frame-mounted`), promote it
 * into view and unmount the old one. The owner sees the preview update in place
 * — no blank, no spinner, no lost visual continuity. (In-app JS state resets —
 * it is genuinely new module code — but the transition is seamless.)
 *
 * INVARIANTS THIS ENCODES
 *  - The old frame survives until the incoming frame is proven mountable, so a
 *    broken bundle (dead import, render hang) never strands the owner on a
 *    spinner — the last working version stays on screen.
 *  - At most ONE extra frame exists at a time (bounded memory on phones): a
 *    newer version supersedes a still-loading incoming rather than stacking.
 *  - No hidden frame is ever created before the live frame has painted: during
 *    first load there is nothing to protect, so a new version just retargets
 *    the single frame (the spinner is already up).
 *
 * The component (AppCanvas) owns the DOM, refs, message plumbing, timers, and
 * the decision of WHICH frame a postMessage came from (source attribution).
 * This module owns only the abstract question "given the frames that exist and
 * an event, what should exist now."
 */

/**
 * @typedef {Object} SwapState
 * @property {string} liveVersion       version of the visible, interactive frame
 * @property {boolean} liveLoaded       has the live frame settled (mounted OR
 *   terminally errored)? drives the first-load spinner overlay
 * @property {string|null} incomingVersion  version of the hidden buffered frame
 *   being loaded for a swap, or null when not swapping
 * @property {number} swaps             count of completed promotions (drives the
 *   "updated" shimmer; 0 means we are still on the first load)
 */

/** @param {string} version @returns {SwapState} */
export function initSwapState(version) {
  return { liveVersion: version, liveLoaded: false, incomingVersion: null, swaps: 0 }
}

/**
 * @param {SwapState} state
 * @param {{type: string, version?: string}} event
 * @returns {SwapState}
 */
export function reduceSwap(state, event) {
  switch (event.type) {
    case 'version': {
      const v = event.version
      if (v === state.incomingVersion) return state          // already buffering it
      if (v === state.liveVersion) {
        // Back to the version already on screen (e.g. a bump then a revert to
        // the same rev). Cancel any in-flight incoming; stay on live.
        return state.incomingVersion == null ? state : { ...state, incomingVersion: null }
      }
      if (!state.liveLoaded) {
        // Nothing has painted yet (first-load spinner is up). There is no
        // visual continuity to protect, so retarget the single frame directly
        // instead of spawning a second iframe we do not need.
        return { ...state, liveVersion: v, incomingVersion: null }
      }
      // The live frame is on screen and interactive: buffer the new version in
      // a hidden incoming frame and keep showing live until it mounts. A newer
      // version supersedes any still-loading incoming — only ONE extra frame.
      return { ...state, incomingVersion: v }
    }

    case 'frame-mounted': {
      if (event.version === state.incomingVersion) {
        // The buffered version rendered — swap it into view and drop the old
        // live frame. This is the moment the owner sees the update land.
        return {
          liveVersion: state.incomingVersion,
          liveLoaded: true,
          incomingVersion: null,
          swaps: state.swaps + 1,
        }
      }
      if (event.version === state.liveVersion) {
        // First-load settle: hide the loading overlay. No swap happened.
        return state.liveLoaded ? state : { ...state, liveLoaded: true }
      }
      return state                                           // stale/unknown frame

    }

    case 'frame-error': {
      if (event.version === state.incomingVersion) {
        // Failed swap. KEEP the old live frame visible and interactive; discard
        // the incoming. The owner keeps a working preview; the next version
        // bump gets a fresh attempt. (The broken frame's own error panel lives
        // inside the hidden iframe and is never shown — the working app is.)
        return { ...state, incomingVersion: null }
      }
      if (event.version === state.liveVersion) {
        // First-load terminal error with nothing to fall back to: hide the
        // overlay so the frame's OWN error panel becomes visible (existing
        // behaviour — the panel is what the owner should see here).
        return state.liveLoaded ? state : { ...state, liveLoaded: true }
      }
      return state
    }

    case 'incoming-timeout':
      // Parent-side safety net: an incoming frame that received init (so its own
      // 10s "no init from parent" timeout can NEVER fire) but never posted
      // frame-mounted — e.g. a render hang. Same outcome as an incoming
      // frame-error: discard it, keep the old live frame. Bounds the hidden
      // frame's lifetime so a wedged bundle can't leak an iframe indefinitely.
      if (event.version === state.incomingVersion) return { ...state, incomingVersion: null }
      return state

    case 'live-reload':
      // The visible frame's DOCUMENT re-loaded at an unchanged version (crash
      // refresh, browser-forced reload — detected as a second onLoad for an
      // already-loaded frame; never a swap, which mounts a NEW version). Its
      // rendered app is gone, so bring the loading overlay back until the
      // fresh document settles via frame-mounted / frame-error — otherwise
      // the owner stares at a blank frame with no loading state. Any in-flight
      // incoming is untouched (its swap resolves independently). A reloading
      // INCOMING frame needs no state change — it is not what's on screen.
      if (event.version === state.liveVersion && state.liveLoaded) {
        return { ...state, liveLoaded: false }
      }
      return state

    default:
      return state
  }
}

/**
 * Deterministic total order over version keys for stable iframe DOM slots.
 *
 * Correctness needs only DETERMINISM: if every frame's DOM position is a pure
 * function of its (immutable-per-frame) version, React never has to reparent a
 * surviving iframe when a sibling is removed — and reparenting a sandboxed
 * iframe reloads its document, which would throw away the freshly-loaded module
 * mid-swap. Version keys are `appVersionKey(updated_at)` — digit strings — so
 * length-then-lexicographic is also monotonic (longer digit string = larger
 * number), which keeps the newer/incoming frame stacked on top; that ordering
 * is a nicety for the fade, not load-bearing.
 *
 * @param {string} a @param {string} b @returns {number}
 */
export function compareVersions(a, b) {
  const sa = String(a)
  const sb = String(b)
  if (sa.length !== sb.length) return sa.length - sb.length
  return sa < sb ? -1 : sa > sb ? 1 : 0
}

// How long the parent waits for a hidden incoming frame to post frame-mounted
// before giving up on the swap and keeping the old frame. Matches the frame's
// own 10s init-timeout budget (app-frame.html) so the two agree.
export const INCOMING_SWAP_TIMEOUT_MS = 10000
