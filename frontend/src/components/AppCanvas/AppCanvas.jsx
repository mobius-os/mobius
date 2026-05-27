import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { appQueries, themeQueries } from '../../hooks/queries.js'
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
// Three message types, all gated on `e.origin === window.location.origin`:
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
//   3. {type: 'moebius:frame-theme', themeCss, bg}         parent → frame
//      Fired when the active theme changes (SSE `theme_updated` event
//      bubbles through useTheme into the React Query cache, this
//      component's `theme` value updates, and we postMessage the new
//      CSS so the iframe refreshes without remounting — preserves
//      in-app state).
//
// Why token-free frame URL: `GET /api/apps/{id}/frame?v={version}` is
// unauthenticated and served `Cache-Control: immutable`. Token arrives
// via postMessage so the SW + browser cache can keep the HTML across
// sessions. See `mobius/CLAUDE.md` "App iframe LRU cache + postMessage
// protocol" for the broader context.
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
  appId, version = 0, appName,
  onNavPush, onNavPop, onNavReset,
}) {
  const { data: token } = appQueries.token.useQuery(appId)

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
    // Gate on theme being resolved too. Sending init with
    // `themeCss: undefined` lets the iframe render its first paint
    // with the fallback theme, then `frame-theme` arrives later and
    // re-paints — a visible flash on cold cache. The iframe's own
    // "Loading…" state is already shown until init arrives, so
    // deferring init by a few ms costs nothing.
    if (!theme) return
    const iframe = iframeRef.current
    if (!iframe || !iframe.contentWindow) return
    iframe.contentWindow.postMessage(
      {
        type: 'moebius:frame-init',
        token,
        themeCss: theme.css,
        bg: theme.bg,
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

  if (!token) return null

  // Token NOT in URL anymore — sent via postMessage above. Frame URL
  // is stable per appId (no `?v=` query). Cache freshness is handled
  // by the server's ETag + the browser's HTTP cache: every iframe
  // mount sends If-None-Match and the server returns 304 (use cache)
  // or 200 (fresh). The `version` prop still drives the iframe `key`
  // below so an `app_updated` event triggers a real React remount,
  // which forces the browser to re-validate the fresh-fetched URL.
  const src = api.apps.frameUrl(appId)

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
          <div className="canvas-loading__spinner" />
          {appName && (
            <div className="canvas-loading__name">{appName}</div>
          )}
        </div>
      )}
    </div>
  )
}
