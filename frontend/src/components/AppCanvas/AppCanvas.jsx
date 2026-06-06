import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, getToken } from '../../api/client.js'
import { appQueries, themeQueries } from '../../hooks/queries.js'
import useOnlineStatus from '../../hooks/useOnlineStatus.js'
import { liveAppToken, resolveLatchedToken } from '../../lib/appToken.js'
import { WifiOff } from 'lucide-react'
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
// Core message types, all gated on `e.origin === window.location.origin`:
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
//      Fired by the frame AFTER `createRoot.render()` returns. Parent
//      hides the loading overlay only on this signal — `iframe.onLoad`
//      is too early (document loaded ≠ React rendered).
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
// Intra-app nav (`moebius:nav-push` / `nav-pop` / `nav-push-ack` /
// `nav-push-rejected` / `nav-back`) is handled below — see the
// `onMessage` handler. These wire the iframe's own back-stack into
// the shell's pushState so device-back unwinds in-app routes first.
//
// Shell-level messages (handled by Shell.jsx, NOT this file):
//   - {type: 'moebius:app-error', appId, error, chatId?}    frame → shell
//   - {type: 'moebius:new-chat', draft?}                    frame → shell
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

// `version` is bumped by Shell when an `app_updated` event arrives
// for this app, busting the iframe cache and forcing a fresh frame
// load (the frame HTML includes the theme CSS, so it needs to refetch
// when the agent updates either the app or the theme).
//
// The app token is cached via the query layer so navigating away
// from the canvas and back doesn't fetch a fresh token, which
// previously cycled the iframe `key` and triggered a full app
// reload (~1–3s of visible jank). Tokens are short-lived but stable
// across React remounts — a 5-minute staleTime is well within the
// server-side validity window.
export default function AppCanvas({
  appId, version = 0, appName, offlineCapable = false,
  onNavPush, onNavPop, onNavReset,
}) {
  // The app-scoped token is fetched from the server and isn't available
  // offline (short-lived; not persisted). Offline, fall back to the
  // owner JWT from localStorage so an offline-capable app still renders
  // and its /api/storage/apps/{id} calls authenticate (owner can reach
  // any app's storage). Without this the `if (!token) return null` gate
  // below short-circuits offline and the app shows nothing. The iframe
  // is same-origin and can already read this JWT (documented sandbox
  // trade-off), so passing it is not a new exposure. Online behavior is
  // unchanged: the app-scoped token wins as soon as the query resolves.
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
  //   • offline → use the owner JWT from localStorage so a fully-cached
  //     offline-capable app still boots. The iframe is same-origin and can
  //     already read this JWT (documented sandbox trade-off) — not a new
  //     exposure.
  // The old gate used navigator.onLine, which on a cold offline reopen left
  // `token` undefined → the `if (!token)` branch rendered blank below despite
  // the app being fully cached. See lib/appToken.js for the full rationale and
  // the on-device-confirmed flap bug the latch fixes.
  const liveToken = liveAppToken(appToken, online, getToken())

  // Latch the token so an `online` oscillation (stale navigator.onLine on
  // Android PWAs) can't revoke a token we already resolved and unmount the live
  // iframe. The latch lives at MODULE scope keyed by appId+version (not a
  // useRef) so it survives an AppCanvas REMOUNT during the flap — the on-device
  // log showed the token dropping to NONE-blank after mount, i.e. the component
  // remounted and a useRef would have reset. Synchronous read/write, so no
  // effect-timing window leaks a stale latch across an app switch.
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

  const [loaded, setLoaded] = useState(false)
  const iframeRef = useRef(null)
  // Synchronous mirror of "iframe has fired its load event." We can't
  // read `loaded` inside `sendInit` because that closure captures the
  // render-time value; we need a ref so the LATEST render path can
  // see the load state without waiting for the next render.
  const loadedRef = useRef(false)

  // Reset state whenever the iframe key changes (new app or version
  // bump). Without this, navigating to a different app would briefly
  // show the previous app's "loaded" state.
  useEffect(() => {
    setLoaded(false)
    loadedRef.current = false
  }, [appId, version])

  // Send init to the iframe. Idempotent on the iframe side — its
  // own `initialized` flag dedups. We do NOT track sent-state on the
  // parent because if the iframe genuinely reloads (DOM reparenting,
  // browser forced reload, etc.) its `initialized` flag resets and
  // it needs a fresh init. A parent-side dedup would silently drop
  // the re-init and the iframe would hit its 10s timeout.
  function sendInit() {
    if (!loadedRef.current) return
    if (!token) return
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
    const iframe = iframeRef.current
    if (!iframe || !iframe.contentWindow) return
    iframe.contentWindow.postMessage(
      {
        type: 'moebius:frame-init',
        token,
        themeCss: theme?.css,
        bg: theme?.bg,
      },
      window.location.origin,
    )
  }

  // Re-attempt init when token or theme becomes available. Covers
  // (a) iframe finished loading before token resolved, (b) iframe
  // finished loading + token resolved before theme cache populated.
  // The iframe's own `initialized` flag dedups any extras, so it's
  // safe to depend on identity-churn-prone theme fields here.
  useEffect(() => {
    sendInit()
  }, [token, appId, version, theme?.css, theme?.bg])

  // Listen for the frame's `frame-mounted` signal, which fires AFTER
  // the React component is rendered inside the iframe. This is the
  // correct moment to hide the loading overlay — `iframe.onLoad`
  // alone fires too early (just the document's load event, before
  // module import + render). Registered ONCE per appId mount so
  // there's no race with the message arriving before the listener.
  useEffect(() => {
    if (!appId) return
    function onMessage(e) {
      if (e.origin !== window.location.origin) return
      const iframe = iframeRef.current
      if (!iframe || e.source !== iframe.contentWindow) return
      const msg = e.data
      if (!msg || typeof msg !== 'object') return
      if (msg.type === 'moebius:frame-mounted' && String(msg.appId) === String(appId)) {
        setLoaded(true)
      }
      // The frame hit a terminal load failure (bad import, no token, no
      // default export, init timeout) and is showing its own error panel.
      // Hide the loading overlay so that panel is visible — without this
      // the opaque spinner covers it and the app looks like a dead,
      // never-resolving spinner. Same origin + source + appId guards as
      // frame-mounted; the offline panel (rendered when !loaded) is a
      // separate path and is unaffected.
      if (msg.type === 'moebius:frame-error' && String(msg.appId) === String(appId)) {
        setLoaded(true)
      }
      // Mini-app back-nav protocol (see useNavigation.appNavPush /
      // appNavPop). The app announces nested-view enter/exit via
      // postMessage; the shell installs a real top-level history
      // sentinel so Android's swipe-back gesture has something to
      // snapshot for the preview, and routes back-gestures back to
      // the iframe via moebius:nav-back instead of changing the
      // shell view.
      if (msg.type === 'moebius:nav-push') {
        const ok = onNavPush?.(appId)
        // Echo the iframe's optional requestId on both ack and reject
        // so the app can correlate when multiple nav-pushes are in
        // flight. Apps that don't pass a requestId get undefined back
        // and treat the next ack/reject as theirs (backwards compatible
        // with the pre-ack protocol).
        const requestId = msg.requestId
        if (ok === false) {
          // Cap hit (MAX_APP_SENTINELS) or pushState threw. Tell the
          // app so it can correct its own bookkeeping — otherwise its
          // count drifts above the shell's and the next nav-pop pops
          // a sentinel it never owned, breaking back-nav permanently.
          iframe.contentWindow?.postMessage(
            { type: 'moebius:nav-push-rejected', requestId },
            window.location.origin,
          )
        } else {
          // Confirm the sentinel is installed so the app can defer
          // opening its nested view until the OS back-gesture preview
          // would snapshot the previous screen. Without this ack the
          // app has to open optimistically and may render the nested
          // view before the shell's pushState lands — the BFCache then
          // snapshots the nested view and uses it as the back preview
          // (wrong background).
          iframe.contentWindow?.postMessage(
            { type: 'moebius:nav-push-ack', requestId },
            window.location.origin,
          )
        }
      } else if (msg.type === 'moebius:nav-pop') {
        onNavPop?.(appId)
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [appId, onNavPush, onNavPop])

  // Clear this app's pending nav-sentinels when the iframe stops
  // representing the same browsing context. That happens on:
  //   - AppCanvas unmount (LRU eviction, logout)
  //   - appId change (different app in the same AppCanvas slot)
  //   - version bump (app_updated → iframe key changes → DOM
  //     remount with a fresh internal nav stack starting at 0)
  //
  // Without resetting, the shell's per-app sentinel count outlives
  // the iframe's internal state, and later back-gestures fire
  // moebius:nav-back postMessages into an iframe whose own nav
  // stack is empty — silently consumed or mishandled by the app.
  //
  // Browser history entries from earlier appNavPush calls remain in
  // history. Once the shell count is 0, _anyAppHasSentinels returns
  // false so popstate skips interception and back-gestures through
  // those orphan entries fall through to native handling.
  useEffect(() => {
    if (!appId) return
    return () => { onNavReset?.(appId) }
  }, [appId, version, onNavReset])

  // Broadcast theme updates to an already-loaded iframe so it can
  // refresh its theme without remounting (and losing app state).
  useEffect(() => {
    if (!loadedRef.current || !iframeRef.current || !theme) return
    iframeRef.current.contentWindow?.postMessage(
      {
        type: 'moebius:frame-theme',
        themeCss: theme.css,
        bg: theme.bg,
      },
      window.location.origin,
    )
  }, [theme?.css, theme?.bg])

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
          <div className="canvas-loading__spinner" />
          {appName && <div className="canvas-loading__name">{appName}</div>}
        </div>
      </div>
    )
  }

  // Token NOT in URL anymore — sent via postMessage above. `v` is in the
  // URL because offline-capable apps are also cached by the service worker:
  // without a versioned cache key, a cold/unknown-connectivity SW can serve
  // a stale frame/module even after the backend updated the app.
  const src = `${api.apps.frameUrl(appId)}?v=${encodeURIComponent(version)}`

  // The iframe key intentionally OMITS `token` — the token may
  // refresh (after staleTime) but the iframe should keep its in-app
  // state. Only `appId` and `version` should force a remount.
  return (
    <div className="canvas-wrap">
      <iframe
        ref={iframeRef}
        key={`${appId}-${version}`}
        className="canvas"
        src={src}
        title={appName || 'Mini-app'}
        data-app-id={appId}
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-top-navigation-by-user-activation"
        allow="microphone"
        onLoad={() => {
          // Per the HTML spec, the iframe's `load` event fires after
          // every <script type="module"> has executed. The frame's
          // message listener is registered at the top of its module
          // script, so by the time we get here it's live and ready
          // to receive init via a single postMessage — no race, no
          // retry. If the token isn't ready yet, the effect above
          // will catch up when it resolves.
          //
          // We do NOT setLoaded(true) here — the loading overlay
          // hides only when the frame posts `frame-mounted`, which
          // fires AFTER the React component renders inside the
          // iframe. iframe.onLoad fires too early (document loaded
          // ≠ app rendered).
          loadedRef.current = true
          sendInit()
        }}
      />
      {!loaded && (
        <div className="canvas-loading" aria-live="polite">
          {!online && !offlineCapable ? (
            // Non-offline-capable apps are never cached by the SW, so
            // offline their frame + module can't load and the spinner
            // would hang forever (a blank screen). Show why instead.
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
            <>
              <div className="canvas-loading__spinner" />
              {appName && (
                <div className="canvas-loading__name">{appName}</div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}
