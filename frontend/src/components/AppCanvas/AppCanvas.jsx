import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiFetch, BASE } from '../../api/client.js'
import { appTokenQueryKey, themeQueryKey } from '../../hooks/queries.js'
import './AppCanvas.css'

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
export default function AppCanvas({ appId, version = 0, appName }) {
  const { data: token } = useQuery({
    queryKey: appTokenQueryKey(appId),
    enabled: !!appId,
    queryFn: async () => {
      const res = await apiFetch('/auth/app-token', {
        method: 'POST',
        body: JSON.stringify({ app_id: appId }),
      })
      if (!res.ok) throw new Error(`app-token ${res.status}`)
      const data = await res.json()
      return data.token
    },
    staleTime: 5 * 60_000,
  })

  // Read the cached theme so we can hand it to the iframe via
  // postMessage on its ready signal. useTheme in Shell handles the
  // fetch and SSE-driven updates; here we just subscribe to cache.
  const { data: theme } = useQuery({
    queryKey: themeQueryKey,
    enabled: false,
    staleTime: Infinity,
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

  // Re-attempt init when token becomes available. Covers the case
  // where the iframe finished loading before the token query
  // resolved — `onLoad` already fired with no init sent; this effect
  // catches up once the token arrives.
  useEffect(() => {
    sendInit()
  }, [token, appId, version])

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
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [appId])

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
  // is stable per (appId, version), enabling the SW frame cache.
  const src = `${BASE}/api/apps/${appId}/frame?v=${version}`

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
