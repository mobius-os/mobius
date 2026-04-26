import { useState, useEffect } from 'react'
import { PersistQueryClientProvider } from '@tanstack/react-query-persist-client'
import { useIsRestoring } from '@tanstack/react-query'
import SetupWizard from './components/SetupWizard/SetupWizard.jsx'
import LoginForm from './components/LoginForm/LoginForm.jsx'
import Shell from './components/Shell/Shell.jsx'
import { apiFetch, getToken, setSetupInProgress } from './api/client.js'
import { queryClient, persistOptions } from './queryClient.js'

export default function App() {
  return (
    <PersistQueryClientProvider client={queryClient} persistOptions={persistOptions}>
      <AppRoot />
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
  // Fast path: if we have a token, show Shell immediately.
  // The splash covers the initial render so there's no flash.
  const hasToken = !!getToken()
  const [status, setStatus] = useState(hasToken ? 'shell' : 'loading')

  useEffect(() => {
    // shell-reload: skip splash entirely, go straight to shell
    const shellReload = sessionStorage.getItem('shell-reload')
    if (shellReload) {
      const splash = document.getElementById('splash')
      if (splash) splash.remove()
      setStatus('shell')
      return
    }

    // Only check setup status if we don't have a token.
    if (hasToken) {
      removeSplash()
      return
    }
    apiFetch('/auth/setup/status')
      .then((r) => r.json())
      .then((data) => {
        if (!data.configured) {
          setStatus('setup')
        } else {
          setStatus('login')
        }
      })
      .catch(() => setStatus('login'))
      .finally(removeSplash)
  }, [])

  if (status === 'loading' || isRestoring) return null
  if (status === 'setup') return <SetupWizard onDone={() => { setSetupInProgress(false); setStatus('shell') }} />
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
