import { lazy, Suspense, useState, useEffect } from 'react'
import { PersistQueryClientProvider } from '@tanstack/react-query-persist-client'
import { QueryClientProvider, useIsRestoring } from '@tanstack/react-query'
import ErrorBoundary from './components/ErrorBoundary/ErrorBoundary.jsx'
import { api, beginEphemeralAuth, getToken, setToken, BASE } from './api/client.js'
import * as setupSession from './lib/setupSession.js'
import { setupQueries } from './hooks/queries.js'
import { queryClient, persistOptions } from './queryClient.js'
import { shellReload } from './lib/shellReloadState.js'
import { beginEmbedBootstrap } from './lib/chatEmbedBootstrap.js'

// These flows are mutually exclusive. Keep setup, login, the full shell, and
// the opaque embed out of one another's startup path; first boot should not
// parse the chat/editor/chart stack just to show the account form.
const SetupWizard = lazy(() => import('./components/SetupWizard/SetupWizard.jsx'))
const LoginForm = lazy(() => import('./components/LoginForm/LoginForm.jsx'))
const Shell = lazy(() => import('./components/Shell/Shell.jsx'))
const ChatEmbed = lazy(() => import('./components/ChatEmbed/ChatEmbed.jsx'))

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
if (EMBED_ROUTE) {
  beginEphemeralAuth()
  beginEmbedBootstrap()
}

// Validate a ?return= target: same-origin in-app path only. Rejects
// backslashes (browsers normalize '/\\evil' to '//evil' -> open redirect),
// absolute/cross-origin URLs, and protocol-relative '//'. Returns the safe
// path+query+hash, or null.
function safeReturnPath(raw) {
  if (!raw) return null
  if (!raw.startsWith('/')) return null            // absolute in-app path only
  if (raw.includes('\\') || /%5c/i.test(raw)) return null  // (encoded) backslash -> // -> open redirect
  let u
  try { u = new URL(raw, window.location.origin) } catch { return null }
  if (u.origin !== window.location.origin) return null
  if (!u.pathname.startsWith('/') || u.pathname.startsWith('//')) return null
  if (decodeURIComponent(u.pathname).includes('\\')) return null  // belt-and-suspenders
  return u.pathname + u.search + u.hash
}

export default function App() {
  if (EMBED_ROUTE) {
    return (
      <QueryClientProvider client={queryClient}>
        <ErrorBoundary label="chat-embed">
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
  // If a previous tab created an account but the user closed before
  // finishing the provider step, resume the wizard there
  // instead of dropping them into a Shell with no AI configured.
  // Read BEFORE the token fast path so we don't briefly mount Shell.
  const hasToken = !!getToken()
  const resumeStep = hasToken ? setupSession.getResumeStep() : null
  let ssoSignal = ''
  try {
    const params = new URLSearchParams(window.location.search)
    if (params.get('mobius_sso') === '1') ssoSignal = 'handoff'
    if (params.get('mobius_sso_error') === '1') ssoSignal = 'error'
  } catch { /* ignore */ }
  const initialStatus = resumeStep
    ? 'setup'
    : (hasToken
        ? 'shell'
        : (ssoSignal === 'handoff'
            ? 'sso'
            : (ssoSignal === 'error' ? 'sso-error' : 'loading')))
  const [status, setStatus] = useState(initialStatus)
  const setupStatusQuery = setupQueries.status.useQuery({
    enabled: !hasToken && !ssoSignal,
  })
  // Stable across renders — we only need the value captured on mount.
  const [initialSetupStep, setInitialSetupStep] = useState(resumeStep || 'account')

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
          setupSession.setInProgress(true)
          setupSession.saveStep('provider')
          setInitialSetupStep('provider')
          setStatus('setup')
          return
        }
        const ret = safeReturnPath(data.return_path)
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
    let ret
    try { ret = safeReturnPath(new URLSearchParams(window.location.search).get('return')) } catch { return }
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
      // Either resuming setup or going to shell — both already set
      // synchronously above. Just hide the splash.
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

  if (status === 'loading' || isRestoring) return null
  if (status === 'sso') return <RouteLoading label="Signing in to Möbius" />
  if (status === 'sso-error') return (
    <ManagedSignInError
      onRetry={() => window.location.replace(api.auth.sso.startUrl('/shell/'))}
    />
  )
  if (status === 'setup-error') return (
    <SetupStatusError
      retrying={setupStatusQuery.isFetching}
      onRetry={() => setupStatusQuery.refetch()}
    />
  )
  if (status === 'setup') return (
    <Suspense fallback={<RouteLoading label="Loading setup" />}>
      <SetupWizard
        initialStep={initialSetupStep}
        onDone={() => {
          setupSession.clearResumeStep()
          setupSession.setInProgress(false)
          setStatus('shell')
        }}
      />
    </Suspense>
  )
  if (status === 'login') return (
    <Suspense fallback={<RouteLoading label="Loading sign in" />}>
      <LoginForm onLogin={() => {
        let ret
        try { ret = safeReturnPath(new URLSearchParams(window.location.search).get('return')) } catch { /* ignore */ }
        if (ret) { window.location.replace(ret); return }
        setStatus('shell')
      }} />
    </Suspense>
  )
  return (
    <Suspense fallback={<RouteLoading label="Loading Möbius" />}>
      <Shell />
    </Suspense>
  )
}

function RouteLoading({ label }) {
  return (
    <div className="app-route-loading" role="status" aria-label={label} />
  )
}

function SetupStatusError({ retrying, onRetry }) {
  return (
    <div className="errbound" role="alert">
      <div className="errbound__card">
        <h1 className="errbound__title">Couldn’t reach Möbius</h1>
        <p className="errbound__body">
          The server didn’t answer the startup check. Your account status is unknown, so sign-in is paused until the connection recovers.
        </p>
        <div className="errbound__actions">
          <button
            type="button"
            className="errbound__btn errbound__btn--primary"
            onClick={onRetry}
            disabled={retrying}
          >
            {retrying ? 'Trying again…' : 'Try again'}
          </button>
        </div>
      </div>
    </div>
  )
}

function ManagedSignInError({ onRetry }) {
  return (
    <div className="errbound" role="alert">
      <div className="errbound__card">
        <h1 className="errbound__title">Couldn’t sign in</h1>
        <p className="errbound__body">
          Your Möbius account could not be confirmed. Try again from this browser.
        </p>
        <div className="errbound__actions">
          <button
            type="button"
            className="errbound__btn errbound__btn--primary"
            onClick={onRetry}
          >
            Try again
          </button>
        </div>
      </div>
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
