import {
  api,
  getToken,
  OWNER_TOKEN_CHANGED_EVENT,
  setToken,
} from '../api/client.js'
import { isStandaloneDisplay } from '../utils/installPlatform.js'
import { readStandaloneBoot } from './standaloneBoot.js'
import { prepareShellInstallPass } from './shellInstallPass.js'


/** Redeem the copied iOS cookie before React chooses setup, login, or shell. */
export async function redeemInstalledShellSession({
  standaloneApp = () => Boolean(readStandaloneBoot()),
  standaloneShell = isStandaloneDisplay,
  hasToken = () => Boolean(getToken()),
  redeem = options => api.auth.shellInstallPass.redeem(options),
  saveToken = setToken,
} = {}) {
  if (standaloneApp() || !standaloneShell() || hasToken()) return false
  try {
    const response = await redeem()
    if (!response.ok) return false
    const data = await response.json()
    if (!data?.access_token) return false
    saveToken(data.access_token)
    return true
  } catch {
    return false
  }
}


/** Keep one copied-cookie grant ready in an authenticated iOS browser tab. */
export function startShellInstallSessionLifecycle({
  standaloneApp = () => Boolean(readStandaloneBoot()),
  standaloneShell = isStandaloneDisplay,
  hasToken = () => Boolean(getToken()),
  prepare = prepareShellInstallPass,
  windowTarget = window,
  documentTarget = document,
} = {}) {
  if (standaloneApp() || standaloneShell()) return () => {}

  const controller = new AbortController()
  let stopped = false
  function refresh({ force = false } = {}) {
    if (stopped || !hasToken()) return
    void prepare({ force, signal: controller.signal })
  }
  function refreshWhenVisible() {
    if (documentTarget.visibilityState === 'visible') refresh({ force: true })
  }
  function refreshAfterSignIn() {
    refresh({ force: true })
  }

  windowTarget.addEventListener('pageshow', refreshAfterSignIn)
  windowTarget.addEventListener(OWNER_TOKEN_CHANGED_EVENT, refreshAfterSignIn)
  documentTarget.addEventListener('visibilitychange', refreshWhenVisible)
  refresh()

  return () => {
    stopped = true
    controller.abort()
    windowTarget.removeEventListener('pageshow', refreshAfterSignIn)
    windowTarget.removeEventListener(OWNER_TOKEN_CHANGED_EVENT, refreshAfterSignIn)
    documentTarget.removeEventListener('visibilitychange', refreshWhenVisible)
  }
}
