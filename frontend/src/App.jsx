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
import useScrollbarAutohide from './hooks/useScrollbarAutohide.js'
import { queryClient, persistOptions } from './queryClient.js'

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

export default function App() {
  useScrollbarAutohide()

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
  // finishing provider/Gemini steps, resume the wizard at that step
  // instead of dropping them into a Shell with no AI configured.
  // Read BEFORE the token fast path so we don't briefly mount Shell.
  const hasToken = !!getToken()
  const resumeStep = hasToken ? setupSession.getResumeStep() : null
  const initialStatus = resumeStep ? 'setup' : (hasToken ? 'shell' : 'loading')
  const [status, setStatus] = useState(initialStatus)
  const setupStatusQuery = setupQueries.status.useQuery({ enabled: !hasToken })
  // Stable across renders — we only need the value captured on mount.
  const [initialSetupStep] = useState(resumeStep || 'account')

  useEffect(() => {
    // shell-reload: skip splash entirely, go straight to shell
    const shellReload = sessionStorage.getItem('shell-reload')
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
  if (status === 'login') return <LoginForm onLogin={() => setStatus('shell')} />
  return <Shell />
}

function removeSplash() {
  const splash = document.getElementById('splash')
  if (splash) {
    splash.style.opacity = '0'
    setTimeout(() => splash.remove(), 400)
  }
}
