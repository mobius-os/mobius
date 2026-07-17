import { useEffect, useLayoutEffect, useReducer, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { appQueries, themeQueries } from '../../hooks/queries.js'
import { serviceSurfaceFrameUrl } from '../../lib/serviceSurface.js'
import useOnlineStatus from '../../hooks/useOnlineStatus.js'
import { liveAppToken, resolveLatchedToken } from '../../lib/appToken.js'
import {
  cacheAppToken, clearAppFrameStorage, readAppFrameStorage,
  readCachedAppToken, removeAppFrameStorage, setAppFrameStorage,
  isSharedVirtualStorageKey,
} from '../../lib/appFrameStorage.js'
import { getEffectiveTheme } from '../../lib/themeService.js'
import { readSafeAreaInsets, zeroInsets } from '../../lib/safeAreaInsets.js'
import { startMicrophoneCapture } from '../../lib/microphoneCapture.js'
import {
  initSwapState, reduceSwap, compareVersions, INCOMING_SWAP_TIMEOUT_MS,
} from '../../lib/previewSwapState.js'
import WifiOff from 'lucide-react/dist/esm/icons/wifi-off.mjs'
import './AppCanvas.css'

// =================================================================
// AppCanvas ↔ iframe postMessage protocol
// =================================================================
// This file is the PARENT (sender). The RECEIVER side lives in
// `frontend/public/app-frame.html` (the inline <script type="module">
// near the bottom). Both sides must move together — adding a message
// type, renaming a field, or changing payload shape requires editing
// both files in the same PR.
//
// Parent messages are accepted only from the shell origin. Frame messages have
// the opaque origin `null` and are attributed to an exact mounted contentWindow.
//
//   1. {type: 'moebius:frame-init', token, themeCss, bg}    parent → frame
//      Fired by `sendInit()` below — on iframe.onLoad AND whenever the
//      token query resolves (covers the case where the iframe loaded
//      before the token was ready). Idempotent — the frame's own
//      `initialized` flag dedups. Parent MUST NOT dedup: a real iframe
//      reload (DOM reparenting, browser forced reload) resets the
//      iframe flag but not parent state, and the re-init must fire or
//      the iframe sits at its 10s loading-timeout.
//
//   2. {type: 'moebius:frame-mounted', appId}              frame → parent
//      Fired by the frame AFTER its first render COMMITS (MountSignal's
//      passive effect in app-frame.html — NOT right after
//      `createRoot.render()` returns, which only schedules the render).
//      Parent hides the loading overlay / promotes a buffered version swap
//      only on this signal — `iframe.onLoad` is too early (document loaded
//      ≠ React rendered), and a render()-returned post would be too early
//      for the swap (a first-render throw aborts the commit; promoting on
//      that lie would unmount the working frame for a dead one).
//
//   2b. {type: 'moebius:frame-error', appId}               frame → parent
//      Fired by the frame on a TERMINAL load failure (bad import, no
//      token, no default export, init timeout) instead of (2). Parent
//      hides the loading overlay so the frame's own error panel shows —
//      otherwise the opaque spinner covers it and the app looks like a
//      dead, never-resolving spinner.
//
//   3. {type: 'moebius:frame-theme', themeCss, bg}         parent → frame
//      Fired when the active theme changes (SSE `theme_updated` event
//      bubbles through useTheme into the React Query cache, this
//      component's `theme` value updates, and we postMessage the new
//      CSS so the iframe refreshes without remounting — preserves
//      in-app state).
//
//   3b. {type: 'moebius:frame-interactivity', interactive} parent → frame
//      Separates "still painted" from "may receive interaction". When the
//      drawer opens over the selected app, the frame stays visible beneath the
//      scrim but synchronously cancels any Android compositor scroll already in
//      flight. Hidden/incoming frames receive false too.
//
// Intra-app nav (`moebius:nav-push` / `nav-pop` / `nav-push-ack` /
// `nav-push-rejected` / `nav-back`) is handled below — see the
// `onMessage` handler. These wire the iframe's own back-stack into
// the shell's pushState so device-back unwinds in-app routes first.
//
//   4. {type: 'moebius:immersive', value, appId}            frame → parent
//      The app asks the shell to hide its chrome (top bar) so the canvas
//      fills the whole viewport — games, primarily. Handled below and
//      forwarded to Shell via the `onImmersive` callback with THIS
//      canvas's appId (the payload's appId is ignored — identity comes
//      from the verified event.source, so a frame can only toggle
//      immersive for itself). Shell applies it only while this app is
//      the active canvas; see lib/immersive.js for the state contract.
//
//   5. {type: 'moebius:frame-insets', insets}              parent → frame
//      The device safe-area insets ({top,right,bottom,left} px strings),
//      forwarded so an immersive (full-bleed, under-the-notch) app can pad
//      away from the notch/home-indicator. env(safe-area-inset-*) reads 0
//      inside the sandboxed iframe (only the top-level document resolves
//      viewport-fit insets), so the shell reads the REAL values off a probe
//      element and posts them; the frame applies them as
//      --mobius-safe-{top,right,bottom,left} on :root. Non-zero only while
//      THIS app is immersive (the `immersive` prop); zeros otherwise, so a
//      windowed app whose chrome already owns the inset padding can't
//      double-pad. See lib/safeAreaInsets.js for the read contract.
//
//   6. {type: 'moebius:app-intent', intent}                parent → frame
//      One-shot shell intent delivered after the app has mounted. Used by
//      catalog surfaces that open an app directly into a setup/settings path
//      without inventing app-specific deep links.
//
//   7. moebius:microphone-*                                bidirectional
//      Opaque frames cannot call getUserMedia themselves. A visible
//      frame requests a bounded capture; the trusted shell records mono PCM and
//      transfers only those samples back to that exact contentWindow.
//
//   8. {type: 'moebius:frame-focus'}                       frame → parent
//      Pointer input inside an opaque iframe cannot bubble into its positioned
//      shell wrapper. The live frame signals the parent so its pane becomes the
//      focused owner of keyboard and immersive state.
//
// Shell-level messages (handled by Shell.jsx, NOT this file):
//   - {type: 'moebius:app-error', appId, error, chatId?}    frame → shell
//   - {type: 'moebius:new-chat', draft?, autoSend?}          frame → shell
//   - {type: 'moebius:open-chat', chatId, draft?}           frame → shell
//   - {type: 'moebius:open-app', appId}                     frame → shell
//     Switch the shell to another installed app. `appId` may be the
//     numeric DB id or the slug; Shell matches against its apps list
//     and silently ignores unknown ids. This is app-SWITCHING (closes
//     the source iframe's view, opens another app's iframe), distinct
//     from `nav-push` which is intra-app routing within one iframe.
//
// Why token-free frame URL: `GET /api/apps/{id}/frame?v={version}` is
// unauthenticated. Token arrives via postMessage so the long-lived JWT
// never appears in frame history; `v` is the app.updated_at cache buster
// that prevents the offline-app service-worker cache from serving an old
// frame/module after an app update.
// =================================================================

// One shared off-screen probe whose four padding sides are set to
// env(safe-area-inset-*); getComputedStyle resolves each env() to a concrete
// px value on the top-level document (the iframe can't — see
// lib/safeAreaInsets.js). Created lazily and reused across every AppCanvas
// instance so the immersive passthrough doesn't churn a DOM node per app.
// During SSR / a non-DOM test env (no document) we return zeros — the helper
// is only meaningfully invoked client-side from the iframe onLoad / immersive
// effect, so the probe is never needed before the DOM exists.
let _insetProbe = null
function readDeviceInsets() {
  if (typeof document === 'undefined') return zeroInsets()
  if (!_insetProbe) {
    _insetProbe = document.createElement('div')
    _insetProbe.setAttribute('aria-hidden', 'true')
    _insetProbe.style.cssText = [
      'position:fixed', 'top:0', 'left:0',
      'width:0', 'height:0', 'visibility:hidden', 'pointer-events:none',
      'padding-top:env(safe-area-inset-top)',
      'padding-right:env(safe-area-inset-right)',
      'padding-bottom:env(safe-area-inset-bottom)',
      'padding-left:env(safe-area-inset-left)',
    ].join(';')
    document.body.appendChild(_insetProbe)
  }
  return readSafeAreaInsets(getComputedStyle(_insetProbe))
}

// Retire the broker session synchronously at shell lifecycle boundaries. The
// control object does not exist while getUserMedia permission is pending, so
// the cancellation flag is load-bearing: the async starter checks it before it
// can announce or deliver anything to a frame that is no longer live.
function cancelMicrophoneCapture(captureRef, { notifyFrame = false } = {}) {
  const capture = captureRef.current
  if (!capture) return
  captureRef.current = null
  capture.cancelled = true
  if (notifyFrame) {
    // A deactivated canvas stays mounted in the shell's LRU. Settle its runtime
    // session so returning to that app can start a new recording; samples and
    // unrelated permission errors remain suppressed by the ownership guards.
    try {
      capture.sourceWindow?.postMessage({
        type: 'moebius:microphone-error',
        requestId: capture.requestId,
        name: 'AbortError',
        message: 'Recording cancelled because the app is no longer active.',
      }, '*')
    } catch {}
  }
  capture.control?.cancel()
}

// The first-open loading surface — the emotional peak of a build, so it's
// accent-forward rather than a generic gray spinner: the app name is present,
// and shimmer skeleton bars hint a header + list are on their way. Shared by
// both loading branches (no-token and online-fetching) so they never drift.
// Presentational only; the 80ms fade-in guard lives on .canvas-loading.
function CanvasLoadingBrand({ appName }) {
  return (
    <>
      <div className="canvas-loading__spinner" />
      {appName && <div className="canvas-loading__name">{appName}</div>}
      <div className="canvas-loading__skeleton" aria-hidden="true">
        <div className="canvas-loading__bar canvas-loading__bar--head" />
        <div className="canvas-loading__bar" />
        <div className="canvas-loading__bar canvas-loading__bar--short" />
      </div>
    </>
  )
}

// `version` is bumped by Shell when an `app_updated` event arrives for this
// app (a recompile advanced app.updated_at). Rather than remount the one iframe
// on every bump — which blanked the running preview to a full-frame spinner and
// dropped all in-app state on each ~1s agent save — we DOUBLE-BUFFER the swap:
// keep the current frame visible and load the new version in a hidden frame
// alongside it, then swap it in only once it has actually rendered. The abstract
// state machine lives in lib/previewSwapState.js (pure + unit-tested); this
// component owns the DOM, refs, message routing, and timers. See the
// "Double-buffered version swap" block in the component body.
//
// TWO FRAMES ARE ALIVE DURING A SWAP. Every consequence of that is load-bearing:
//   - Messages are attributed to the frame that sent them by comparing
//     e.source to each frame's contentWindow — never assume one contentWindow.
//   - `data-app-id` is set on the VISIBLE frame only, so useNavigation's
//     `iframe[data-app-id="…"]` selector resolves the frame the user sees.
//   - Nav-sentinel and immersive callbacks route to the VISIBLE frame only.
//   - Frames render in a version-deterministic DOM order so React never
//     reparents a survivor (a sandboxed-iframe reparent = reload = the module
//     we just loaded is thrown away).
//
// The app token is cached via the query layer so navigating away from the
// canvas and back doesn't fetch a fresh token, which previously cycled the
// iframe `key` and triggered a full app reload (~1–3s of visible jank). Tokens
// are short-lived but stable across React remounts — a 5-minute staleTime is
// well within the server-side validity window. The token is app-scoped (keyed
// by appId server-side), so it is identical for both buffered versions.
export default function AppCanvas({
  appId, version = 0, appName, appSlug, offlineCapable = false,
  immersive = false,
  // Whether this app is the currently-visible canvas (canvas view + active
  // app). One prop, two consumers:
  //   - `moebius:frame-visibility` — the shell keeps recently-used apps
  //     MOUNTED and merely toggles `visibility:hidden` on the inactive ones
  //     (Shell.css .shell__view), which does NOT change a nested iframe's
  //     Page Visibility state, so the verdict is forwarded to the frame and
  //     an app can pause background work (audio, rAF, timers) on navigate-away.
  //   - Immersive gating — Shell's immersive holder is GLOBAL
  //     last-writer-wins, so immersive intent may only be forwarded/replayed
  //     while this canvas is active; otherwise a hidden cached app (or its
  //     freshly-promoted rebuild) steals chrome/insets from the app on screen.
  // Defaults true so any caller that omits it keeps apps un-paused (back-compat).
  active = true,
  // Whether this app is the active tab of ANY visible pane (design §5). Drives
  // the frame-visibility signal and the nav-push gate — an app visible in a
  // background split still runs and can install nested-view sentinels. `active`
  // (the FOCUSED pane's app) stays focused-pane-only and continues to gate
  // safe-area insets + the immersive holder (global last-writer-wins). Defaults
  // to `active` so a single-pane caller (where visible === focused) is unchanged.
  visible = active,
  // Whether the live frame may receive direct interaction. This differs from
  // `visible` while the shell drawer is open: each pane remains painted behind
  // the scrim, but compositor momentum inside its iframe must be cancelled so
  // background content cannot keep coasting beneath the drawer.
  interactive = visible,
  pendingIntent = null,
  onNavPush, onNavPop, onNavReset, onAppFocus, onImmersive, onIntentDelivered, onAppError,
}) {
  const queryClient = useQueryClient()
  const [serviceSurface, setServiceSurface] = useState(null)
  const serviceRequestRef = useRef(0)
  const serviceFrameRef = useRef(null)
  const microphoneCaptureRef = useRef(null)
  // Fresh app tokens are persisted for their remaining short lifetime so a
  // fully cached app can cold-boot offline. The cache is app-id scoped and JWT
  // claims are checked on read; the long-lived owner token never enters a frame.
  const { data: appToken } = appQueries.token.useQuery(appId)
  // Real reachability (probes /api/health), NOT navigator.onLine — which reads
  // a stale "true" on a COLD offline reopen (close the PWA offline, reopen,
  // open an app from the drawer): the SW serves the shell from cache so the
  // browser never makes a real network attempt to update the flag.
  const online = useOnlineStatus()
  // Token choice, gated ONLY on real reachability:
  //   • online  → wait for the app-scoped token; NEVER substitute the owner
  //     JWT (keeps the long-lived owner JWT out of the module URL/history).
  //     We deliberately do NOT fall back on the token query's error state: a
  //     transient server error while ONLINE must not leak the owner JWT, and
  //     React Query pauses the query offline so its error is unreliable there.
  //   • offline → use a still-valid cached token for this exact app.
  // The old gate used navigator.onLine, which on a cold offline reopen left
  // `token` undefined → the `if (!token)` branch rendered blank below despite
  // the app being fully cached. See lib/appToken.js for the full rationale and
  // the on-device-confirmed flap bug the latch fixes.
  // Expiry is an online authorization boundary, not an offline cache boundary.
  // The cached module key excludes the token and reconnect mints a fresh one;
  // keeping the expired scoped token offline preserves the installation nonce
  // without ever falling back to the long-lived owner credential.
  const cachedAppToken = readCachedAppToken(
    appId,
    undefined,
    Date.now(),
    { allowExpired: !online },
  )
  const liveToken = liveAppToken(appToken, online, cachedAppToken)

  useEffect(() => {
    if (appToken) cacheAppToken(appId, appToken)
  }, [appId, appToken])

  // Latch the token so an `online` oscillation (stale navigator.onLine on
  // Android PWAs) can't revoke a token we already resolved and unmount the live
  // iframe. The latch lives at MODULE scope keyed by appId+version (not a
  // useRef) so it survives an AppCanvas REMOUNT during the flap — the on-device
  // log showed the token dropping to NONE-blank after mount, i.e. the component
  // remounted and a useRef would have reset. Synchronous read/write, so no
  // effect-timing window leaks a stale latch across an app switch.
  //
  // Double-buffering keeps two versions of the SAME app alive, but the token is
  // app-scoped (identical for both), so we resolve it ONCE — keyed on the newest
  // requested `version` (the prop), exactly as the single-iframe code did. That
  // means the latch tracks the INCOMING version continuously through the swap
  // window, so the frame we promote already has its token latched (no gap at the
  // instant of promotion). resolveLatchedToken's "drop older versions of the
  // same app" step harmlessly forgets the outgoing version's latch — that frame
  // is already initialized and about to unmount — and because we never resolve
  // for two versions in one render, the two live frames can never fight over the
  // latch. See lib/appToken.js.
  const token = resolveLatchedToken(appId, version, liveToken, appToken)

  // AppCanvas was passive (enabled: false) — relied on Shell's
  // useTheme to write the cache. After ticket 047, AppCanvas owns
  // its own fetch on cache miss so deep-link arrival (or query
  // eviction) does not wedge sendInit forever waiting for a
  // populate that never comes.
  const { data: theme } = useQuery({
    queryKey: themeQueries.keys.all,
    queryFn: themeQueries.fetch,
  })

  // ── Double-buffered version swap ─────────────────────────────────
  // The pure state machine (lib/previewSwapState.js) decides which frames must
  // exist: the live (visible) frame, plus a hidden incoming frame during a swap.
  // It never touches the DOM — this component maps its state onto iframes below.
  const [swap, dispatchSwap] = useReducer(reduceSwap, version, initSwapState)

  // version -> HTMLIFrameElement for every currently-mounted frame (one, or two
  // during a swap). Message routing and targeted posts look frames up here BY
  // VERSION — a Map, not a single ref, because two iframes are alive during the
  // swap window and each must be addressable independently.
  const framesRef = useRef(new Map())
  // Versions whose iframe has fired its document `load` event — i.e. its message
  // listener is live and it can receive frame-init/theme/insets. Per frame,
  // because the two buffered frames finish loading independently.
  const loadedDocsRef = useRef(new Set())
  // version -> last immersive intent (bool) that frame declared. Recorded for
  // EVERY frame, including a hidden incoming one whose real-time immersive post
  // is withheld (only the visible frame drives chrome live). On a swap we replay
  // the promoted frame's recorded intent so an immersive game stays immersive
  // across a rebuild without a chrome flash.
  const frameImmersiveRef = useRef(new Map())
  // A STABLE callback-ref per version. React only invokes a callback ref when
  // its identity changes, so caching one per version means a frame's ref never
  // churns across re-renders (a churning ref would delete+re-add the Map entry
  // and could mis-route a message that arrived in that window). The closure
  // captures its own version, so unmount (el === null) prunes exactly that frame.
  const refCbCacheRef = useRef(new Map())
  function frameRefCb(v) {
    const cache = refCbCacheRef.current
    let cb = cache.get(v)
    if (!cb) {
      cb = (el) => {
        if (el) framesRef.current.set(v, el)
        else {
          // AppCanvas can temporarily render a service/loading surface without
          // unmounting itself. Ref removal is the exhaustive boundary for the
          // exact live document, including those non-component teardown paths.
          const capture = microphoneCaptureRef.current
          if (v === liveVersionRef.current && capture?.sourceVersion === v) {
            cancelMicrophoneCapture(microphoneCaptureRef)
          }
          // A service-surface takeover can remove the live iframe without
          // unmounting AppCanvas or advancing the buffered version. Retire its
          // host sentinels at this exhaustive document-removal boundary too.
          if (v === liveVersionRef.current) onNavReset?.(appId)
          framesRef.current.delete(v)
          loadedDocsRef.current.delete(v)
          frameImmersiveRef.current.delete(v)
          cache.delete(v)
        }
      }
      cache.set(v, cb)
    }
    return cb
  }

  // The VISIBLE frame's version (and this canvas's active verdict), mirrored
  // into refs so the long-lived message listener (registered once,
  // deliberately minimal deps) can gate visible-frame-only concerns — nav
  // sentinels and immersive — without going stale. Assigned during render:
  // idempotent, no side effect.
  const liveVersionRef = useRef(swap.liveVersion)
  liveVersionRef.current = swap.liveVersion
  const activeRef = useRef(active)
  activeRef.current = active
  const interactiveRef = useRef(interactive)
  interactiveRef.current = interactive
  const visibleRef = useRef(visible)
  visibleRef.current = visible

  function postToFrame(v, message) {
    // A sandboxed frame without allow-same-origin has an opaque origin, so the
    // browser requires "*" here. Trust is provided by the exact contentWindow
    // checks on replies plus the frame's parent-origin check on receipt.
    framesRef.current.get(v)?.contentWindow?.postMessage(message, '*')
  }

  // Send the init handshake to ONE frame. Idempotent on the frame side — its own
  // `initialized` flag dedups. We deliberately do NOT track sent-state on the
  // parent: a genuine iframe reload (DOM reparenting, browser forced reload)
  // resets the frame's flag but not parent state, and the re-init MUST fire or
  // the frame sits at its 10s loading-timeout. Each buffered frame runs its own
  // handshake.
  function sendInit(v) {
    if (!loadedDocsRef.current.has(v)) return
    if (!token) return
    const win = framesRef.current.get(v)?.contentWindow
    if (!win) return
    // NOTE: do NOT gate on `theme`. Previously this returned early until the
    // theme query resolved, to avoid a one-frame flash from the iframe's
    // fallback theme repainting when `frame-theme` arrives. But offline (cold
    // reopen) the theme query can be slow/undefined, and gating on it meant
    // `frame-init` was never sent → the iframe mounted but its React never
    // booted → it sat on the frame's own "Loading…" spinner until the 10s
    // init-timeout. That spinner-forever (and the slow offline load) is far
    // worse than a brief theme flash. So we send init as soon as the token is
    // ready; the frame applies its fallback theme and a later `frame-theme`
    // message (the effect below + the theme-broadcast effect) repaints it.
    //
    // Send the CURRENTLY-APPLIED shell theme, not the `/api/theme` query result.
    // getEffectiveTheme() reads what the shell already painted onto its own DOM
    // (the `<style id="mobius-theme">` block + data-theme), present even when the
    // theme query is unresolved (cold offline reopen) — whereas `theme?.css` is
    // `undefined` there, and an `undefined` themeCss makes the frame's
    // applyTheme() a no-op so a stale/dark-injected cached frame STAYS dark.
    const eff = getEffectiveTheme()
    win.postMessage(
      {
        type: 'moebius:frame-init',
        token,
        themeCss: eff?.css ?? theme?.css,
        bg: eff?.bg ?? theme?.bg,
        storage: readAppFrameStorage(appId, undefined, appSlug),
      },
      '*',
    )
  }

  // Keep the swap state machine fed by the `version` prop. On mount this
  // dispatch is a no-op (the reducer was lazily initialised from the same
  // version); it only does work when a recompile bumps the prop.
  useEffect(() => {
    dispatchSwap({ type: 'version', version })
  }, [version])

  // Re-attempt init on every mounted-but-loaded frame when token or theme
  // becomes available. Covers (a) an iframe that finished loading before the
  // token resolved, (b) token resolved before the theme cache populated, and
  // (c) a freshly-mounted incoming frame during a swap. Each frame's own
  // `initialized` flag dedups extras, so depending on identity-churn-prone
  // theme fields is safe.
  useEffect(() => {
    for (const v of framesRef.current.keys()) sendInit(v)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, appId, appSlug, version, theme?.css, theme?.bg])

  // Parent-side load timeout for the HIDDEN incoming frame. The frame's own 10s
  // "no init from parent" timeout cannot fire once we deliver init (its
  // `initialized` flag flips true), so a bundle that imports fine but then hangs
  // in render would leave the incoming frame buffered forever. If it doesn't
  // mount within the budget, treat it as a failed swap: discard the incoming and
  // keep the OLD live frame visible (the reducer decides; see incoming-timeout).
  useEffect(() => {
    if (swap.incomingVersion == null) return
    const v = swap.incomingVersion
    const id = setTimeout(() => {
      dispatchSwap({ type: 'incoming-timeout', version: v })
    }, INCOMING_SWAP_TIMEOUT_MS)
    return () => clearTimeout(id)
  }, [swap.incomingVersion])

  // Single message listener for BOTH buffered frames. Registered once per appId
  // mount (deliberately minimal deps: it reads live state through refs +
  // dispatch, never through render-scope closures, so it never needs
  // re-registering and never goes stale). The `frame-mounted` signal fires AFTER
  // the React component renders inside the iframe — the correct moment to hide
  // the overlay / promote a swap; `iframe.onLoad` alone is too early (document
  // loaded ≠ React rendered).
  useEffect(() => {
    if (!appId) return
    function onMessage(e) {
      if (e.origin !== 'null' && e.origin !== window.location.origin) return
      const msg = e.data
      if (!msg || typeof msg !== 'object') return
      // ATTRIBUTE the message to the exact frame that sent it. Two frames are
      // alive during a swap, so we must NOT assume a single contentWindow —
      // compare e.source to each mounted frame's window and recover its version.
      let srcVersion = null
      for (const [v, el] of framesRef.current) {
        if (el?.contentWindow && el.contentWindow === e.source) { srcVersion = v; break }
      }
      if (srcVersion == null) return   // not one of our frames (stale/unknown)

      // Opaque frames get a synchronous in-memory localStorage facade. Persist
      // mutations into an app-private namespace only; never write arbitrary
      // keys into the shell's own storage.
      if (msg.type === 'moebius:storage-set') {
        const saved = setAppFrameStorage(appId, msg.key, msg.value)
        if (saved && isSharedVirtualStorageKey(msg.key)) {
          window.dispatchEvent(new CustomEvent('mobius:shared-storage', {
            detail: { key: msg.key, value: msg.value },
          }))
        }
        return
      }
      if (msg.type === 'moebius:storage-remove') {
        const removed = removeAppFrameStorage(appId, msg.key)
        if (removed && isSharedVirtualStorageKey(msg.key)) {
          window.dispatchEvent(new CustomEvent('mobius:shared-storage', {
            detail: { key: msg.key, value: null },
          }))
        }
        return
      }
      if (msg.type === 'moebius:storage-clear') {
        clearAppFrameStorage(appId)
        return
      }

      // frame-mounted: the reducer routes it — promotion if it's the incoming
      // frame, first-load settle if it's the live frame, ignored if stale.
      if (msg.type === 'moebius:frame-mounted' && String(msg.appId) === String(appId)) {
        dispatchSwap({ type: 'frame-mounted', version: srcVersion })
        return
      }
      // frame-error: a TERMINAL load failure (bad import, no token, no default
      // export, init timeout). If it's the HIDDEN incoming frame, the reducer
      // discards it and keeps the OLD working frame visible — the owner is never
      // stranded on a broken swap. If it's the first-load frame (nothing working
      // to fall back to), the reducer hides the overlay so the frame's own error
      // panel becomes visible (existing behaviour).
      if (msg.type === 'moebius:frame-error' && String(msg.appId) === String(appId)) {
        dispatchSwap({ type: 'frame-error', version: srcVersion })
        return
      }
      // Token-expiry recovery. The frame detects a 401/403 on the module import
      // probe and posts this instead of a permanent error panel. We invalidate
      // the app-token query so React Query refetches a fresh token; sendInit
      // then fires on the next token change and the frame re-receives frame-init.
      // The token is app-scoped (shared by both buffered versions), so a single
      // invalidate serves whichever frame reported the expiry. The frame resets
      // its own `initialized` flag before posting, so it accepts the follow-up.
      if (msg.type === 'moebius:token-expired' && String(msg.appId) === String(appId)) {
        appQueries.token.invalidate(queryClient, appId)
        return
      }

      // App-runtime crash report. Source attribution decides its fate right
      // here — the single, local place that knows which frame is hidden.
      // Forward ONLY the LIVE frame's crash up to the shell (onAppError);
      // SWALLOW a hidden, not-yet-promoted incoming frame's. A failed
      // double-buffer swap is usually a broken build (that is WHY it failed),
      // and the swap state machine already keeps the old working frame live —
      // so a hidden frame must not plant a crash-report draft or yank the view
      // to a chat while the owner's visible preview still works. This replaces
      // the module-global incomingFrames WeakSet that coupled AppCanvas and
      // Shell for exactly this guard.
      if (msg.type === 'moebius:app-error' && String(msg.appId) === String(appId)) {
        if (srcVersion === liveVersionRef.current) {
          onAppError?.(appId, msg.error, msg.chatId)
        }
        return
      }

      // Immersive intent. Record it for EVERY frame — including a hidden
      // incoming one — so the replay effect can apply the visible frame's
      // intent whenever it becomes the one on screen (promotion, or this
      // canvas becoming active). Forward LIVE only when this frame is both
      // the visible frame AND this canvas is the active one: Shell's
      // immersive holder is global last-writer-wins, so a hidden cached
      // app's post would otherwise steal chrome/insets from the app the
      // user is actually looking at. Forward with THIS canvas's appId, not
      // msg.appId — the source check already proved identity, and trusting
      // the payload would let any frame toggle immersive for a different
      // app. `=== true` keeps the wire contract strictly boolean (truthy
      // garbage reads as a release, the safe direction).
      if (msg.type === 'moebius:immersive') {
        const value = msg.value === true
        frameImmersiveRef.current.set(srcVersion, value)
        if (srcVersion === liveVersionRef.current && activeRef.current) {
          onImmersive?.(appId, value)
        }
        return
      }

      // Everything below is a concern of the VISIBLE frame only. Ignore it from
      // a hidden incoming frame: it isn't interactive and shouldn't emit these,
      // but a mount-time nav-push must never install a shell history sentinel for
      // a frame the user cannot see (the sentinel would belong to the wrong
      // browsing context). Route acks back to the source frame via e.source
      // directly — it is the verified sender window.
      if (srcVersion !== liveVersionRef.current) return

      if (msg.type === 'moebius:frame-focus') {
        onAppFocus?.(appId)
        return
      }

      if (msg.type === 'moebius:microphone-start') {
        // Reject, rather than truncate, an invalid correlation id. Truncation
        // makes every response impossible for the requesting runtime to match.
        const requestId = typeof msg.requestId === 'string' && msg.requestId.length <= 120
          ? msg.requestId
          : ''
        const sourceWindow = e.source
        if (!visibleRef.current || !requestId) return
        if (microphoneCaptureRef.current) {
          sourceWindow.postMessage({
            type: 'moebius:microphone-error', requestId,
            name: 'InvalidStateError', message: 'Another recording is already in progress.',
          }, '*')
          return
        }

        const pending = {
          requestId, sourceWindow, sourceVersion: srcVersion,
          control: null, stopRequested: false, cancelled: false,
        }
        microphoneCaptureRef.current = pending
        ;(async () => {
          try {
            const control = await startMicrophoneCapture({
              maxSeconds: msg.maxSeconds,
              onLevel(level) {
                if (microphoneCaptureRef.current !== pending
                    || pending.cancelled
                    || !visibleRef.current
                    || pending.sourceVersion !== liveVersionRef.current) return
                sourceWindow.postMessage({
                  type: 'moebius:microphone-level', requestId, level,
                }, '*')
              },
            })
            pending.control = control
            if (microphoneCaptureRef.current !== pending
                || pending.cancelled
                || !visibleRef.current
                || pending.sourceVersion !== liveVersionRef.current) {
              control.done.catch(() => {})
              control.cancel()
              return
            }
            sourceWindow.postMessage({
              type: 'moebius:microphone-started', requestId,
              sampleRate: control.sampleRate,
            }, '*')
            if (pending.stopRequested) control.stop()

            const result = await control.done
            const mayDeliver = microphoneCaptureRef.current === pending
              && !pending.cancelled
              && visibleRef.current
              && pending.sourceVersion === liveVersionRef.current
            if (microphoneCaptureRef.current === pending) microphoneCaptureRef.current = null
            if (!mayDeliver) return
            const samples = result.samples
            try {
              sourceWindow.postMessage({
                type: 'moebius:microphone-result', requestId,
                sampleRate: result.sampleRate, samples,
              }, '*', [samples.buffer])
            } catch {
              sourceWindow.postMessage({
                type: 'moebius:microphone-result', requestId,
                sampleRate: result.sampleRate, samples,
              }, '*')
            }
          } catch (error) {
            const mayDeliver = microphoneCaptureRef.current === pending
              && !pending.cancelled
              && visibleRef.current
              && pending.sourceVersion === liveVersionRef.current
            if (microphoneCaptureRef.current === pending) microphoneCaptureRef.current = null
            if (!mayDeliver || error?.name === 'AbortError') return
            sourceWindow.postMessage({
              type: 'moebius:microphone-error', requestId,
              name: error?.name || 'Error',
              message: error?.message || 'Microphone recording failed.',
            }, '*')
          }
        })()
        return
      }

      if (msg.type === 'moebius:microphone-stop' || msg.type === 'moebius:microphone-cancel') {
        const capture = microphoneCaptureRef.current
        if (!capture || capture.sourceWindow !== e.source || capture.requestId !== msg.requestId) return
        if (msg.type === 'moebius:microphone-cancel') {
          cancelMicrophoneCapture(microphoneCaptureRef)
        } else if (capture.control) {
          capture.control.stop()
        } else {
          capture.stopRequested = true
        }
        return
      }

      if (msg.type === 'moebius:open-service') {
        const slug = typeof msg.service === 'string' ? msg.service : ''
        // A mini-app may only ask for the same-named installed service. The
        // shell—not the opaque frame—resolves the private configured origin.
        if (!activeRef.current || !slug || slug !== appSlug) return
        const requestId = ++serviceRequestRef.current
        setServiceSurface({ slug, phase: 'checking', url: null, error: null })
        ;(async () => {
          try {
            const surface = await api.services.surface(slug)
            if (serviceRequestRef.current !== requestId) return
            const url = new URL(surface.url)
            const expectedPath = `/services/${encodeURIComponent(slug)}/_mobius/surface`
            if (url.origin === window.location.origin || url.pathname !== expectedPath) {
              throw new Error('Service origin must be separate from Möbius.')
            }
            const correlation = globalThis.crypto?.randomUUID?.()
              || `${Date.now()}-${Math.random().toString(36).slice(2)}`
            setServiceSurface({
              slug, phase: 'loading', url: url.href, origin: url.origin,
              correlation, error: null,
            })
          } catch (error) {
            if (serviceRequestRef.current !== requestId) return
            const message = error?.message || 'Service is unavailable.'
            setServiceSurface({ slug, phase: 'error', url: null, error: message })
            try { e.source?.postMessage({ type: 'moebius:service-error', service: slug, error: message }, '*') } catch {}
          }
        })()
        return
      }

      // Mini-app back-nav protocol (see useNavigation.appNavPush / appNavPop).
      // The app announces nested-view enter/exit; the shell installs a real
      // top-level history sentinel so Android's swipe-back has something to
      // snapshot, and routes back-gestures to the iframe via moebius:nav-back.
      if (msg.type === 'moebius:nav-push') {
        // A cached frame remains mounted while another app/chat/settings is
        // visible. It may retire an entry it already owns, but it must never
        // install a NEW top-level history entry while off-screen. Gate on
        // VISIBLE (active tab of any visible pane), not focused — a background
        // split's app is still interactive (contract §3.3.6). The shell's own
        // isVisibleApp re-checks pane ownership as the authority.
        const ok = visibleRef.current ? onNavPush?.(appId) : false
        // Echo the iframe's optional requestId on both ack and reject so the app
        // can correlate when multiple nav-pushes are in flight. Apps that don't
        // pass a requestId get undefined back (backwards compatible).
        const requestId = msg.requestId
        if (ok === false) {
          // Cap hit (MAX_APP_SENTINELS) or pushState threw. Tell the app so it
          // can correct its own bookkeeping — otherwise its count drifts above
          // the shell's and the next nav-pop pops a sentinel it never owned,
          // breaking back-nav permanently.
          e.source.postMessage(
            { type: 'moebius:nav-push-rejected', requestId },
            '*',
          )
        } else {
          // Confirm the sentinel is installed so the app can defer opening its
          // nested view until the OS back-gesture preview would snapshot the
          // previous screen. Without this ack the app opens optimistically and
          // may render the nested view before the shell's pushState lands — the
          // BFCache then snapshots the wrong background as the back preview.
          e.source.postMessage(
            { type: 'moebius:nav-push-ack', requestId },
            '*',
          )
        }
      } else if (msg.type === 'moebius:nav-pop') {
        onNavPop?.(appId)
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [appId, appSlug, onNavPush, onNavPop, onAppFocus, onImmersive, onAppError, queryClient])

  // Captures belong to the exact visible document that requested them. Cancel
  // before paint when the canvas leaves every visible pane or a buffered build is
  // promoted. A still-mounted hidden frame receives only the correlated
  // AbortError needed to settle its runtime session; samples and unrelated
  // permission errors remain gated out.
  useLayoutEffect(() => {
    const capture = microphoneCaptureRef.current
    if (!capture) return
    if (visible && capture.sourceVersion === swap.liveVersion) return
    cancelMicrophoneCapture(microphoneCaptureRef, { notifyFrame: !visible })
  }, [visible, swap.liveVersion])

  useLayoutEffect(() => () => {
    cancelMicrophoneCapture(microphoneCaptureRef)
  }, [])

  useEffect(() => {
    // A response from the previously mounted app must never replace the new
    // app's canvas with a service surface.
    serviceRequestRef.current += 1
    setServiceSurface(null)
  }, [appId, version])

  useEffect(() => {
    if (serviceSurface?.phase !== 'loading') return
    const onReady = (event) => {
      if (event.source !== serviceFrameRef.current?.contentWindow) return
      if (event.origin !== serviceSurface.origin) return
      const message = event.data || {}
      if (message.type !== 'moebius:service-ready'
          || message.service !== serviceSurface.slug
          || message.correlation !== serviceSurface.correlation) return
      setServiceSurface(current => current?.correlation === serviceSurface.correlation
        ? { ...current, phase: 'ready' } : current)
    }
    window.addEventListener('message', onReady)
    const id = setTimeout(() => {
      setServiceSurface(current => current?.phase === 'loading'
        ? { ...current, phase: 'error', error: 'The service did not finish loading.' }
        : current)
    }, 15000)
    return () => {
      clearTimeout(id)
      window.removeEventListener('message', onReady)
    }
  }, [
    serviceSurface?.phase, serviceSurface?.url, serviceSurface?.origin,
    serviceSurface?.slug, serviceSurface?.correlation,
  ])

  // Two setup keys intentionally coordinate catalog apps. Fan those safe,
  // explicit mutations across mounted opaque frames without exposing their
  // private preference namespaces or the shell's auth storage.
  useEffect(() => {
    function onSharedStorage(e) {
      const { key, value } = e.detail || {}
      if (!isSharedVirtualStorageKey(key)) return
      for (const v of framesRef.current.keys()) {
        postToFrame(v, { type: 'moebius:storage-sync', key, value })
      }
    }
    window.addEventListener('mobius:shared-storage', onSharedStorage)
    return () => window.removeEventListener('mobius:shared-storage', onSharedStorage)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Clear this app's pending nav-sentinels when the VISIBLE frame stops
  // representing the same browsing context. That happens on:
  //   - AppCanvas unmount (LRU eviction, logout)
  //   - a SWAP (swap.liveVersion advances → the old frame, with its internal
  //     nav stack, unmounts and a fresh frame starting at 0 takes over)
  //
  // NOTE the dep is `swap.liveVersion`, NOT the raw `version` prop. Under
  // double-buffering a version bump loads a HIDDEN frame and does NOT tear down
  // the live frame; resetting on the prop would wrongly clear the sentinels of a
  // frame the user is still interacting with. Reset only when the visible frame
  // actually changes.
  //
  // Without resetting, the shell's per-app sentinel count outlives the iframe's
  // internal state, and later back-gestures fire moebius:nav-back into an iframe
  // whose own nav stack is empty — silently consumed or mishandled. Once the
  // shell count is 0, _anyAppHasSentinels returns false so popstate skips
  // interception and back-gestures through orphan history entries fall through
  // to native handling.
  useLayoutEffect(() => {
    if (!appId) return
    return () => { onNavReset?.(appId) }
  }, [appId, swap.liveVersion, onNavReset])

  // Drive the shell chrome from the VISIBLE frame's recorded immersive intent,
  // but ONLY while this canvas is the active one. Replays fire on promotion
  // (swap.liveVersion changes — the promoted frame's real-time post was
  // withheld while it was hidden, so an immersive game stays immersive across
  // a rebuild) and on this canvas becoming active (`active` flips true — a
  // hidden frame's post was withheld too, so returning to the game re-enters
  // immersive). A hidden canvas never calls with value:true: Shell's holder is
  // global last-writer-wins and a hidden promotion must not steal chrome from
  // the app on screen. The cleanup release covers deactivation, swap, and
  // unmount alike (a torn-down iframe can't run its own cleanup-post; the
  // recorded intent survives in frameImmersiveRef, so reactivation restores
  // it). Releasing an app that doesn't hold the slot is a no-op
  // (lib/immersive.js). Keyed on swap.liveVersion, not the version prop, for
  // the same reason as the nav reset above (a bump alone must not touch the
  // live frame).
  useEffect(() => {
    if (!appId) return
    if (active) {
      onImmersive?.(appId, frameImmersiveRef.current.get(swap.liveVersion) === true)
    }
    return () => { onImmersive?.(appId, false) }
  }, [appId, swap.liveVersion, active, onImmersive])

  // Broadcast theme updates to every loaded frame (live + any incoming) so each
  // refreshes its theme without remounting (and losing app state).
  // Prefer the query result (`theme.css`): it is this effect's trigger and is
  // guaranteed fresh here. useTheme's applyThemeToDom and this broadcast are both
  // passive effects with no guaranteed ordering, so on an SSE/agent refetch
  // getEffectiveTheme() may still read the PRE-change DOM — using it first would
  // broadcast a stale css. Fall back to the applied-DOM theme only when theme.css
  // is absent (offline/unresolved), the offline-safe source (the toggle path
  // applies it before invalidating, so the DOM is fresh).
  useEffect(() => {
    if (!theme) return
    const eff = getEffectiveTheme()
    for (const v of framesRef.current.keys()) {
      if (!loadedDocsRef.current.has(v)) continue
      postToFrame(v, {
        type: 'moebius:frame-theme',
        themeCss: theme.css ?? eff?.css,
        bg: theme.bg ?? eff?.bg,
      })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme?.css, theme?.bg])

  // One-shot shell intent, delivered to the VISIBLE frame once it has mounted.
  // Re-delivers to the promoted frame after a swap (dep on swap.liveVersion):
  // the new module didn't receive the intent the old one did.
  useEffect(() => {
    if (!pendingIntent || !swap.liveLoaded) return
    if (!framesRef.current.get(swap.liveVersion)?.contentWindow) return
    postToFrame(swap.liveVersion, {
      type: 'moebius:app-intent',
      intent: pendingIntent.intent,
      nonce: pendingIntent.nonce,
    })
    onIntentDelivered?.(appId, pendingIntent)
  }, [appId, swap.liveLoaded, swap.liveVersion, pendingIntent, onIntentDelivered])

  // ── P1-A: probed-online forwarding ──────────────────────────────
  // Forward the shell's real reachability verdict (from useOnlineStatus, which
  // probes /api/health) into the app iframe. The runtime's window.mobius.online
  // previously returned raw navigator.onLine, which is stale on Android PWAs
  // (reads 'true' while genuinely offline). By posting the probed verdict we
  // give apps accurate connectivity without requiring them to probe themselves.
  //
  // Sent once on iframe load (via sendOnlineStatus called in onLoad) and again
  // whenever `online` changes — so apps always see the current verdict.
  // The iframe may not be loaded yet when `online` first changes; the onLoad
  // handler is the init path, the useEffect below is the update path.
  //
  // Standalone context (routes/standalone.py) has no AppCanvas, so the
  // runtime's navigator.onLine fallback is the only signal there — graceful.
  function sendOnlineStatus(v) {
    postToFrame(v, { type: 'moebius:online-status', online })
  }

  useEffect(() => {
    for (const v of framesRef.current.keys()) {
      if (loadedDocsRef.current.has(v)) sendOnlineStatus(v)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [online])

  // ── Immersive safe-area passthrough (.pm/128 follow-up) ──────────
  // Forward the device safe-area insets so an immersive (full-bleed) app can
  // pad away from the notch/home-indicator. env(safe-area-inset-*) reads 0
  // inside the sandboxed iframe, so the shell reads the real values off a
  // probe element and posts them; the frame applies them to :root as
  // --mobius-safe-*. Only non-zero while THIS app is immersive — a windowed
  // app's chrome already owns the inset padding, so it must receive zeros and
  // not double-pad. Sent on iframe load (sendInsets in onLoad), whenever the
  // immersive verdict flips, and on resize/orientationchange while immersive
  // (a rotation moves the cutout, so the cached insets would otherwise go
  // stale — see the geometry-change effect below).
  // Insets are non-zero ONLY for the VISIBLE frame while it is immersive. A
  // hidden incoming frame is never the immersive holder, so it gets zeros; a
  // windowed frame whose chrome already owns the inset padding also gets zeros
  // so it can't double-pad.
  function sendInsets(v) {
    const insets = (v === liveVersionRef.current && immersive) ? readDeviceInsets() : zeroInsets()
    postToFrame(v, { type: 'moebius:frame-insets', insets })
  }

  // ── In-shell foreground/background signal ────────────────────────
  // Forward whether THIS app is the visible canvas. The shell keeps recently-
  // used apps mounted and hides the inactive ones with `visibility:hidden` on a
  // shell ancestor — which does NOT change the nested iframe's
  // document.visibilityState (no visibilitychange/blur/pagehide fires). So an
  // app that plays audio or animates keeps running after you navigate away
  // (the reported "music keeps playing after exiting" bug). Posting the verdict
  // lets an app pause on `visible:false` and resume on `visible:true`. The
  // native Page Visibility API still covers real tab-hide (it propagates to
  // same-origin child frames), so the two compose: an app is foreground only
  // when it's the active canvas AND the tab is visible.
  //
  // Double-buffer routing: only the LIVE frame ever receives the real verdict.
  // A buffered incoming frame is invisible by construction, so it gets an
  // explicit `visible:false` at load (handleFrameLoad) — a rebuild that boots
  // in the hidden buffer must not start audio/rAF work before promotion. The
  // effect below re-sends on every `active` flip AND on promotion
  // (swap.liveVersion change), so a freshly-promoted frame immediately learns
  // whether it is foreground.
  function sendVisibility(v, visible) {
    postToFrame(v, { type: 'moebius:frame-visibility', visible })
  }

  function sendInteractivity(v, enabled, visible = visibleRef.current) {
    postToFrame(v, {
      type: 'moebius:frame-interactivity',
      interactive: enabled,
      suspendScrolling: visible && !enabled,
    })
  }

  useEffect(() => {
    if (loadedDocsRef.current.has(swap.liveVersion)) {
      sendVisibility(swap.liveVersion, visible)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, swap.liveVersion])

  // Layout timing is deliberate. A drawer-open render removes the shell canvas
  // from hit-testing and sends this message before paint; app-frame.html then
  // cancels any already-running Android kinetic scroll inside the iframe. A
  // passive effect left one more compositor frame for Klix's long document to
  // coast visibly beneath the newly-open drawer.
  useLayoutEffect(() => {
    if (loadedDocsRef.current.has(swap.liveVersion)) {
      sendInteractivity(swap.liveVersion, interactive, visible)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interactive, visible, swap.liveVersion])

  useEffect(() => {
    for (const v of framesRef.current.keys()) {
      if (loadedDocsRef.current.has(v)) sendInsets(v)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [immersive, swap.liveVersion])

  // Re-forward insets on viewport geometry change WHILE immersive. Rotating the
  // device or a window resize moves the notch/home-indicator (landscape puts
  // the cutout on a side, so top→0 and left/right gain the inset), but the
  // effect above only fires on the immersive FLIP — without this the app keeps
  // padding for the pre-rotation orientation and a control slides under the
  // cutout. Windowed apps receive zeros and don't change on resize, so the
  // listener is only attached while immersive (and torn down on exit). The
  // probe element resolves the fresh env() values after layout settles.
  useEffect(() => {
    if (!immersive) return
    function onGeometryChange() { sendInsets(liveVersionRef.current) }
    window.addEventListener('resize', onGeometryChange)
    window.addEventListener('orientationchange', onGeometryChange)
    return () => {
      window.removeEventListener('resize', onGeometryChange)
      window.removeEventListener('orientationchange', onGeometryChange)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [immersive])

  if (!appId) {
    return (
      <div className="canvas canvas--empty">
        <p className="canvas__hint">
          Open the menu to switch apps, or chat to create one.
        </p>
      </div>
    )
  }

  if (!token) {
    // No token yet. With the token logic above this only happens while
    // ONLINE and the app-scoped token is still fetching (offline always has
    // the owner-JWT fallback). Render the loading spinner rather than null so
    // there is never a blank frame — a cold reopen shows a spinner that
    // resolves into the app, not a black screen.
    return (
      <div className="canvas-wrap">
        <div className="canvas-loading" aria-live="polite">
          <CanvasLoadingBrand appName={appName} />
        </div>
      </div>
    )
  }

  if (serviceSurface) {
    const loaded = serviceSurface.phase === 'ready'
    const retryService = () => setServiceSurface(current => {
      if (!current?.url) return null // remount wrapper only after explicit Retry
      const correlation = globalThis.crypto?.randomUUID?.()
        || `${Date.now()}-${Math.random().toString(36).slice(2)}`
      return { ...current, phase: 'loading', correlation, error: null }
    })
    return (
      <div className="canvas-wrap" data-service-surface={serviceSurface.slug}>
        {serviceSurface.url && ['loading', 'ready'].includes(serviceSurface.phase) && (
          <iframe
            ref={serviceFrameRef}
            className="canvas canvas--live"
            style={{ opacity: loaded ? 1 : 0 }}
            src={serviceSurfaceFrameUrl(serviceSurface.url, serviceSurface.correlation)}
            title={appName || serviceSurface.slug}
            sandbox="allow-scripts allow-forms allow-popups allow-downloads allow-same-origin allow-top-navigation-by-user-activation"
            allow="clipboard-read; clipboard-write; fullscreen"
          />
        )}
        {!loaded && (
          <div className="canvas-loading" aria-live="polite">
            {['error', 'closed'].includes(serviceSurface.phase) ? (
              <div className="canvas-loading__offline">
                <div className="canvas-loading__offline-title">
                  {serviceSurface.phase === 'closed'
                    ? `${appName || serviceSurface.slug} is closed`
                    : `Couldn’t open ${appName || serviceSurface.slug}`}
                </div>
                <div className="canvas-loading__offline-detail">
                  {serviceSurface.phase === 'closed'
                    ? 'Open it when you’re ready, or switch to another app.'
                    : serviceSurface.error}
                </div>
                <div className="canvas-loading__offline-actions">
                  <button
                    type="button"
                    className="canvas-loading__offline-button canvas-loading__offline-button--primary"
                    onClick={retryService}
                  >
                    {serviceSurface.phase === 'closed'
                      ? `Open ${appName || serviceSurface.slug}` : 'Retry'}
                  </button>
                  {serviceSurface.phase === 'error' && (
                    <button
                      type="button"
                      className="canvas-loading__offline-button canvas-loading__offline-button--secondary"
                      onClick={() => {
                        serviceRequestRef.current += 1
                        setServiceSurface(current => ({ ...current, phase: 'closed' }))
                      }}
                    >Close</button>
                  )}
                </div>
              </div>
            ) : <CanvasLoadingBrand appName={appName} />}
          </div>
        )}
      </div>
    )
  }

  // Token NOT in the URL — sent via postMessage above. `v` IS in the URL: it is
  // the SW's offline cache key (a cold/unknown-connectivity SW could otherwise
  // serve a stale frame after a backend update). frameRev folds in the shared
  // app-frame.html content hash so a frame-only redeploy busts every app's frame.
  const frameRev =
    (typeof document !== 'undefined' &&
      document.querySelector('meta[name="mobius-frame-rev"]')?.content) || ''
  function frameSrc(v) {
    return `${api.apps.frameUrl(appId)}?v=${encodeURIComponent(v)}${frameRev ? '-' + frameRev : ''}`
  }

  function handleFrameLoad(v) {
    // Per the HTML spec, the iframe's `load` event fires after every
    // <script type="module"> has executed, so the frame's message listener is
    // live by now — a single init postMessage reaches it, no race, no retry. We
    // do NOT mark the swap "loaded" here: the loading overlay hides only when the
    // frame posts `frame-mounted` (after React commits inside it); onLoad is too
    // early (document loaded ≠ app rendered).
    //
    // A SECOND load event for a version already marked loaded means this
    // frame's document genuinely RELOADED (crash refresh, browser-forced
    // reload — never a swap, which mounts a new version in its own iframe).
    // The old document and its rendered app are gone; if this is the visible
    // frame the owner is looking at a blank document, so tell the reducer to
    // bring the loading overlay back until the fresh document settles. The
    // re-init below then runs the fresh document's handshake (this is exactly
    // why the parent never dedups frame-init).
    if (loadedDocsRef.current.has(v)) {
      if (v === liveVersionRef.current) cancelMicrophoneCapture(microphoneCaptureRef)
      dispatchSwap({ type: 'live-reload', version: v })
    }
    loadedDocsRef.current.add(v)
    sendInit(v)
    sendOnlineStatus(v)
    sendInsets(v)
    // A booting incoming frame is invisible by construction and must not
    // start audio/rAF work before promotion, so it learns `visible:false`
    // here; the live frame gets the real visible verdict. Promotion re-sends
    // via the [visible, swap.liveVersion] effect above. Interactivity is a
    // separate gate (drawer-open momentum cancel); its "painted" argument tracks
    // `visible` post active->visible split, its enabled argument tracks
    // `interactive` (focused pane, drawer-aware).
    sendVisibility(v, v === liveVersionRef.current ? visibleRef.current : false)
    sendInteractivity(
      v,
      v === liveVersionRef.current ? interactiveRef.current : false,
      v === liveVersionRef.current ? visibleRef.current : false,
    )
  }

  // The frames to render: the live (visible) frame, plus a hidden incoming frame
  // during a swap. ORDER MUST be a pure function of version (never of live vs
  // incoming ROLE): a frame's DOM slot then never changes across renders, so
  // React never reparents a surviving iframe when the outgoing one is removed —
  // and reparenting a sandboxed iframe reloads its document, throwing away the
  // module we just loaded. See compareVersions.
  const frameVersions = [swap.liveVersion]
  if (swap.incomingVersion != null) frameVersions.push(swap.incomingVersion)
  frameVersions.sort(compareVersions)

  // The iframe key OMITS `token` (a token refresh must not remount and drop
  // in-app state) — only appId+version identify a frame. During a swap the
  // incoming frame keeps the same key when promoted (only its className flips),
  // so it is NOT reloaded at the moment it becomes visible.
  return (
    <div className="canvas-wrap">
      {frameVersions.map((v) => {
        const isLive = v === swap.liveVersion
        return (
          <iframe
            ref={frameRefCb(v)}
            key={`${appId}-${v}`}
            className={`canvas ${isLive ? 'canvas--live' : 'canvas--incoming'}`}
            src={frameSrc(v)}
            title={appName || 'Mini-app'}
            // data-app-id marks the app's VISIBLE browsing context. Exactly one
            // frame carries it (the live one) at every observable moment, so
            // useNavigation's `iframe[data-app-id="…"]` selector always resolves
            // the frame the user sees — even mid-swap. The hidden incoming frame
            // withholds it.
            data-app-id={isLive ? appId : undefined}
            data-frame-version={v}
            sandbox="allow-scripts allow-forms allow-popups allow-top-navigation-by-user-activation"
            allow="microphone; fullscreen"
            allowFullScreen
            onLoad={() => handleFrameLoad(v)}
          />
        )
      })}
      {/* One-shot "updated" shimmer on a successful swap. Keyed on the SWAP
          COUNT — not the live version and not gated on liveLoaded — so it
          remounts (replays) exactly when a promotion lands, and a live-frame
          reload (which drops liveLoaded without a swap) neither replays nor
          interrupts it. Suppressed on first load (swaps === 0). Opacity-only +
          pointer-events:none, so it never blocks input or reflows. */}
      {swap.swaps > 0 && (
        <div className="canvas-swap-flash" key={`flash-${swap.swaps}`} aria-hidden="true" />
      )}
      {!swap.liveLoaded && (
        <div className="canvas-loading" aria-live="polite">
          {!online && !offlineCapable ? (
            // A non-offline-capable app whose frame + module aren't cached
            // yet (never opened while online) can't load offline, so the
            // spinner would hang forever (a blank screen). Show why instead.
            // Note: the SW DOES cache frame/module ungated for every app once
            // opened (every 200 stored) — the `offline_capable` flag only
            // gates the separate standalone-navigation cache + offline write
            // semantics, not this in-shell read path. So this branch is the
            // not-yet-cached case for a non-capable app, not "never cached."
            <div className="canvas-loading__offline">
              <WifiOff className="canvas-loading__offline-icon" aria-hidden="true" />
              <div className="canvas-loading__offline-title">You're offline</div>
              <div className="canvas-loading__offline-detail">
                {appName ? `${appName} needs a connection to open.`
                         : 'This app needs a connection to open.'}{' '}
                It'll work once you're back online.
              </div>
            </div>
          ) : (
            <CanvasLoadingBrand appName={appName} />
          )}
        </div>
      )}
    </div>
  )
}
