import { useState, useEffect } from 'react'
import { PersistQueryClientProvider } from '@tanstack/react-query-persist-client'
import { useIsRestoring } from '@tanstack/react-query'
import SetupWizard from './components/SetupWizard/SetupWizard.jsx'
import LoginForm from './components/LoginForm/LoginForm.jsx'
import Shell from './components/Shell/Shell.jsx'
import ChatEmbed from './components/ChatEmbed/ChatEmbed.jsx'
import ErrorBoundary from './components/ErrorBoundary/ErrorBoundary.jsx'
import { getToken, BASE } from './api/client.js'
import * as setupSession from './lib/setupSession.js'
import { setupQueries } from './hooks/queries.js'
import { queryClient, persistOptions } from './queryClient.js'
// Import the already-parsed shell-reload value from useNavigation — that
// module-level IIFE already consumed and removed the sessionStorage key, so
// reading it again here would always return null (dead branch). We use the
// exported value directly so there is one reader, not two.
import { shellReload } from './hooks/useNavigation.js'

// True when this SPA load is the stripped-chrome chat embed
// (capability A). The SPA catch-all serves index.html for any non-API
// path, so `/shell/embed/chat` boots the same main.jsx → App. We branch
// here, BEFORE the setup/login/Shell flow, so the embed renders inside
// the same PersistQueryClientProvider (ChatView needs the QueryClient +
// persistence) but with none of the Shell chrome. We prepend Vite's
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
  return (
    <PersistQueryClientProvider client={queryClient} persistOptions={persistOptions}>
      <ErrorBoundary label="app">
        {isEmbedRoute() ? <ChatEmbed /> : <AppRoot />}
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
  const initialStatus = resumeStep ? 'setup' : (hasToken ? 'shell' : 'loading')
  const [status, setStatus] = useState(initialStatus)
  const setupStatusQuery = setupQueries.status.useQuery({ enabled: !hasToken })
  // Stable across renders — we only need the value captured on mount.
  const [initialSetupStep] = useState(resumeStep || 'account')

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
    // `shellReload` is the value parsed at module load by useNavigation's
    // IIFE — the sessionStorage key has already been consumed and removed
    // there; re-reading it here would always be null.
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
      setStatus(setupStatusQuery.data.configured ? 'login' : 'setup')
      removeSplash()
    } else if (setupStatusQuery.isError) {
      setStatus('login')
      removeSplash()
    }
  }, [hasToken, setupStatusQuery.isError, setupStatusQuery.isSuccess, setupStatusQuery.data])

  if (status === 'loading' || isRestoring) return null
  if (status === 'setup') return (
    <SetupWizard
      initialStep={initialSetupStep}
      onDone={() => {
        setupSession.clearResumeStep()
        setupSession.setInProgress(false)
        setStatus('shell')
      }}
    />
  )
  if (status === 'login') return <LoginForm onLogin={() => {
    let ret
    try { ret = safeReturnPath(new URLSearchParams(window.location.search).get('return')) } catch { /* ignore */ }
    if (ret) { window.location.replace(ret); return }
    setStatus('shell')
  }} />
  return <Shell />
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
