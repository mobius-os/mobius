import { lazy, Suspense, useState, useEffect } from 'react'
import { PersistQueryClientProvider } from '@tanstack/react-query-persist-client'
import { QueryClientProvider, useIsRestoring } from '@tanstack/react-query'
import ErrorBoundary from './components/ErrorBoundary/ErrorBoundary.jsx'
import PlatformDegradedNotice from './components/ErrorBoundary/PlatformDegradedNotice.jsx'
import './components/ErrorBoundary/RecoveryPanel.css'
import { api, beginEphemeralAuth, getToken, setToken, BASE } from './api/client.js'
import * as setupSession from './lib/setupSession.js'
import { setupQueries, versionQueries } from './hooks/queries.js'
import { queryClient, persistOptions } from './queryClient.js'
import { shellReload } from './lib/shellReloadState.js'
import { beginEmbedBootstrap } from './lib/chatEmbedBootstrap.js'
import { startInstallPromptCapture } from './lib/installPrompt.js'
import { readInstallPass, withoutInstallPass } from './lib/installPassUrl.js'
import { safeReturnPath } from './lib/safeReturnPath.js'
import { readStandaloneBoot } from './lib/standaloneBoot.js'

// These flows are mutually exclusive. Keep setup, login, the full shell, and
// the opaque embed out of one another's startup path; first boot should not
// parse the chat/editor/chart stack just to show the account form.
const SetupWizard = lazy(() => import('./components/SetupWizard/SetupWizard.jsx'))
const LoginForm = lazy(() => import('./components/LoginForm/LoginForm.jsx'))
const Shell = lazy(() => import('./components/Shell/Shell.jsx'))
const ChatEmbed = lazy(() => import('./components/ChatEmbed/ChatEmbed.jsx'))
const StandaloneApp = lazy(() => import('./components/StandaloneApp/StandaloneApp.jsx'))

// True when this SPA load is the stripped-chrome chat embed
// (capability A). The SPA catch-all serves index.html for any non-API
// path, so `/shell/embed/chat` boots the same main.jsx → App. We branch
// here, BEFORE the setup/login/Shell flow, so the embed renders inside a plain
// QueryClientProvider: ChatView needs the client, but an opaque document must
// not touch the owner's persisted cache. We prepend Vite's
// build-time BASE (with its trailing slash stripped) so the match holds
// if the bundle is ever built under a sub-path; in this repo BASE is '/',
// so the comparison is the literal '/shell/embed/chat'.
function isEmbedRoute() {
  try {
    return window.location.pathname === `${BASE}/shell/embed/chat`
  } catch {
    return false
  }
}

const EMBED_ROUTE = isEmbedRoute()
const STANDALONE_APP = readStandaloneBoot()
if (EMBED_ROUTE) {
  beginEphemeralAuth()
  beginEmbedBootstrap()
} else {
  // Capture Chromium's one-shot install event before setup or sign-in can keep
  // the first-use shell card from mounting.
  startInstallPromptCapture()
}

function clearInstallPassFromUrl() {
  const next = withoutInstallPass(window.location.href)
  if (next) window.history.replaceState(null, '', next)
}

export default function App() {
  if (EMBED_ROUTE) {
    return (
      <QueryClientProvider client={queryClient}>
        <ErrorBoundary
          label="chat-embed"
          recoveryKey="chat-embed:root"
          canAskAgent={false}
        >
          {/* Keep the opaque embed blank until its capability is verified. */}
          <Suspense fallback={null}>
            <ChatEmbed />
          </Suspense>
        </ErrorBoundary>
      </QueryClientProvider>
    )
  }
  return (
    <PersistQueryClientProvider client={queryClient} persistOptions={persistOptions}>
      <ErrorBoundary label="app">
        <AppRoot />
      </ErrorBoundary>
    </PersistQueryClientProvider>
  )
}

function AppRoot() {
  // PersistQueryClientProvider hydrates the cache asynchronously from
  // IndexedDB on cold load. During that window, `useQuery` returns
  // `isPending: true` even for cached queries. We hold the splash up
  // until restoration completes so ChatView's useState initializer
  // sees the hydrated cache (no flash on cold reload).
  const isRestoring = useIsRestoring()
  // Provider setup is deliberately contextual now: a usable Möbius opens
  // immediately and Settings owns agent connections. Ignore the legacy
  // `provider` resume marker left by older first-run flows.
  const hasToken = !!getToken()
  const savedResumeStep = hasToken ? setupSession.getResumeStep() : null
  const resumeStep = savedResumeStep === 'account' ? savedResumeStep : null
  let ssoSignal = ''
  // A one-time install pass on a standalone app's FIRST launch. iOS gives the
  // newly installed web app its own empty storage, so this is the only moment
  // it can inherit the session that installed it. Read it even when this
  // container already has a token so the stale credential is still stripped.
  let installPass = ''
  try {
    const params = new URLSearchParams(window.location.search)
    if (params.get('mobius_sso') === '1') ssoSignal = 'handoff'
    if (params.get('mobius_sso_error') === '1') ssoSignal = 'error'
    installPass = readInstallPass(window.location.search, STANDALONE_APP)
  } catch { /* ignore */ }
  const initialStatus = resumeStep
    ? 'setup'
    : (hasToken
        ? 'shell'
        : (installPass
            ? 'install-pass'
            : (ssoSignal === 'handoff'
                ? 'sso'
                : (ssoSignal === 'error' ? 'sso-error' : 'loading'))))
  const [status, setStatus] = useState(initialStatus)
  // "Continue to the built-in version" hides the notice for THIS mount so the
  // owner can use the working fallback. Deliberately not persisted: any reload
  // re-surfaces it while the platform is still degraded, so it never hides.
  const [degradedDismissed, setDegradedDismissed] = useState(false)
  const setupStatusQuery = setupQueries.status.useQuery({
    enabled: !hasToken && !ssoSignal && status !== 'install-pass',
  })
  // `/api/version` already owns the identity of the tree actually being served
  // (`platform` vs the image-baked floor). Read it once the authenticated shell
  // is selected instead of repurposing the setup-status request: managed SSO
  // handoff deliberately skips that separate account-setup path.
  const servedVersionQuery = versionQueries.current.useQuery({
    enabled: hasToken && status === 'shell' && !STANDALONE_APP,
  })
  useEffect(() => {
    if (installPass && hasToken) clearInstallPassFromUrl()
  }, [installPass, hasToken])
  useEffect(() => {
    if (status !== 'install-pass') return undefined
    let cancelled = false
    // Strip the pass from the URL whichever way this goes: it is spent on
    // redemption, and the address stays in the home-screen icon forever.
    async function redeem() {
      try {
        const response = await api.auth.installPass.redeem(
          installPass, STANDALONE_APP.slug,
        )
        if (!response.ok) throw new Error('INSTALL_PASS_REJECTED')
        const data = await response.json()
        if (!data?.access_token) throw new Error('INSTALL_PASS_REJECTED')
        setToken(data.access_token)
        clearInstallPassFromUrl()
        if (!cancelled) setStatus('shell')
      } catch {
        // An expired or spent pass is not an error worth showing: fall
        // through to the ordinary sign-in, which is where this launch would
        // have landed anyway.
        clearInstallPassFromUrl()
        if (!cancelled) setStatus('loading')
      }
    }
    void redeem()
    return () => { cancelled = true }
  }, [status, installPass])
  useEffect(() => {
    if (status !== 'sso') return undefined
    let cancelled = false
    async function finishSignIn() {
      try {
        const response = await api.auth.sso.consume()
        if (!response.ok) throw new Error('SSO_HANDOFF_FAILED')
        const data = await response.json()
        if (!data?.access_token) throw new Error('SSO_HANDOFF_FAILED')
        setToken(data.access_token)
        removeSplash()
        try {
          const current = new URL(window.location.href)
          current.searchParams.delete('mobius_sso')
          window.history.replaceState(null, '', current.pathname + current.search + current.hash)
        } catch { /* ignore */ }
        if (cancelled) return
        if (data.new_owner) {
          // Managed sign-in already established the owner. Open the product;
          // provider setup belongs in Settings and must not become a second
          // onboarding funnel.
          setupSession.clearResumeStep()
          setupSession.setInProgress(false)
          setStatus('shell')
          return
        }
        const ret = safeReturnPath(data.return_path, window.location.origin)
        if (ret && ret !== '/') {
          window.location.replace(ret)
          return
        }
        setStatus('shell')
      } catch {
        if (!cancelled) {
          removeSplash()
          setStatus('sso-error')
        }
      }
    }
    void finishSignIn()
    return () => {
      cancelled = true
    }
  }, [status])

  useEffect(() => {
    if (status === 'sso-error') removeSplash()
  }, [status])

  // Honor a ?return= target. An installed standalone app (its own PWA,
  // often a SEPARATE storage partition with no token) redirects here for
  // auth; bounce straight back to it instead of mounting the shell over a
  // restored chat. One-shot sessionStorage guard prevents a cross-partition
  // redirect loop. Same-origin in-app paths only (no open-redirect).
  useEffect(() => {
    const ret = safeReturnPath(
      new URLSearchParams(window.location.search).get('return'),
      window.location.origin,
    )
    if (!ret) { try { sessionStorage.removeItem('mobius_return_bounced') } catch { /* ignore */ } return }
    if (!hasToken) return  // no token: the login path honors return post-login
    // Target-scoped one-shot: only suppress a repeat bounce to the SAME
    // target (a cross-partition loop), not future legit returns this tab.
    if (sessionStorage.getItem('mobius_return_bounced') === ret) return
    try { sessionStorage.setItem('mobius_return_bounced', ret) } catch { /* ignore */ }
    window.location.replace(ret)
  }, [hasToken])

  useEffect(() => {
    // shell-reload: skip splash entirely, go straight to shell.
    // shellReloadState parsed and removed the one-shot storage key at module
    // load. App and useNavigation both share that same captured value.
    if (shellReload) {
      const splash = document.getElementById('splash')
      if (splash) splash.remove()
      setStatus('shell')
      return
    }

    if (hasToken) {
      // Clear stale provider-wizard state from pre-contextual onboarding.
      if (savedResumeStep && !resumeStep) setupSession.clearResumeStep()
      removeSplash()
      return
    }
    if (setupStatusQuery.isSuccess) {
      if (setupStatusQuery.data.auth_mode === 'mobius_sso') {
        let returnPath = '/'
        try {
          returnPath = (
            window.location.pathname + window.location.search + window.location.hash
          )
        } catch { /* ignore */ }
        window.location.replace(api.auth.sso.startUrl(returnPath))
        return
      }
      setStatus(setupStatusQuery.data.configured ? 'login' : 'setup')
      removeSplash()
    } else if (setupStatusQuery.isError) {
      setStatus('setup-error')
      removeSplash()
    }
  }, [hasToken, setupStatusQuery.isError, setupStatusQuery.isSuccess, setupStatusQuery.data])

  if (status === 'loading' || isRestoring) {
    return <RouteLoading />
  }
  if (status === 'sso') return <RouteLoading />
  if (status === 'install-pass') return <RouteLoading />
  if (status === 'sso-error') return (
    <StartupError
      title="Couldn’t sign in"
      message="Your Möbius account could not be confirmed. Try again from this browser."
      onRetry={() => window.location.replace(api.auth.sso.startUrl('/shell/'))}
    />
  )
  if (status === 'setup-error') return (
    <StartupError
      title="Couldn’t reach Möbius"
      message="The server didn’t answer the startup check. Your account status is unknown, so sign-in is paused until the connection recovers."
      retrying={setupStatusQuery.isFetching}
      onRetry={() => setupStatusQuery.refetch()}
    />
  )
  if (status === 'setup') return (
    <Suspense fallback={<RouteLoading />}>
      <SetupWizard
        onDone={() => {
          setupSession.clearResumeStep()
          setupSession.setInProgress(false)
          setStatus('shell')
        }}
      />
    </Suspense>
  )
  if (status === 'login') return (
    <Suspense fallback={<RouteLoading />}>
      <LoginForm onLogin={() => {
        const ret = safeReturnPath(
          new URLSearchParams(window.location.search).get('return'),
          window.location.origin,
        )
        if (ret) { window.location.replace(ret); return }
        setStatus('shell')
      }} />
    </Suspense>
  )
  // The platform failed to import and we are serving the built-in copy. Surface
  // it before the shell instead of running silently; "Continue" dismisses to the
  // working fallback. Shell only (a standalone mini-app is not the repair owner).
  if (
    hasToken &&
    status === 'shell' &&
    !STANDALONE_APP &&
    servedVersionQuery.data?.serving_source === 'baked' &&
    !degradedDismissed
  ) {
    return <PlatformDegradedNotice onContinue={() => setDegradedDismissed(true)} />
  }
  return (
    <Suspense fallback={<RouteLoading />}>
      {STANDALONE_APP
        ? <StandaloneApp initialApp={STANDALONE_APP} />
        : <Shell />}
    </Suspense>
  )
}

function RouteLoading() {
  return <div className="app-route-loading" aria-hidden="true" />
}

function StartupError({ title, message, retrying = false, onRetry }) {
  return (
    <div className="errbound" role="alert">
      <section className="recovery-panel recovery-panel--boundary errbound__card">
        <h1 className="recovery-panel__title">{title}</h1>
        <p className="recovery-panel__body">{message}</p>
        <div
          className="recovery-panel__actions"
          aria-busy={retrying ? true : undefined}
        >
          <button
            type="button"
            className="recovery-panel__button recovery-panel__button--primary"
            onClick={onRetry}
            disabled={retrying}
          >
            {retrying ? 'Trying again…' : 'Try again'}
          </button>
        </div>
      </section>
    </div>
  )
}

function removeSplash() {
  const splash = document.getElementById('splash')
  if (splash) {
    // Drop pointer-events as we start the fade: the overlay is fixed at
    // z-index 9999 over the whole viewport and lingers ~400ms after opacity
    // hits 0, so without this it keeps intercepting taps on the login form
    // underneath it during the fade (a fast tap on Sign in lands on the
    // splash instead).
    splash.style.pointerEvents = 'none'
    splash.style.opacity = '0'
    setTimeout(() => splash.remove(), 400)
  }
}
