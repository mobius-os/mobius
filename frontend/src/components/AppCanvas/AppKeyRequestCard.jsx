/* The key request of the app in this window, asked in the moment: a dialog in the middle of the app while the owner has it open. */
import { useSyncExternalStore } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { isSharedBrowserAuth } from '../../api/client.js'
import KeyRequest from '../SettingsView/KeyRequest.jsx'
import { KEYS_QUERY, useAppNames, useKeys } from '../SettingsView/keysQueries.js'
import { requestAppPaused, requestFormKey } from '../../lib/keysSettingsText.js'
import { appKeyRequestsToShow } from '../../lib/keyRequestPlaces.js'
import './AppKeyRequestCard.css'

function subscribeToPageVisibility(onChange) {
  document.addEventListener('visibilitychange', onChange)
  return () => document.removeEventListener('visibilitychange', onChange)
}
const pageVisible = () => typeof document === 'undefined' || document.visibilityState === 'visible'

/**
 * An app's key requests are asked only while the owner is using it: no
 * notification, nothing waiting in Settings. A request the app makes while
 * open shows straight away (the server nudges the Shell, which re-reads the
 * requests); one made while it was closed shows when the owner opens it.
 * Not now is the only way out, so the question is always answered.
 *
 * AppCanvas draws it only in the owner's own shell, over a visible app
 * (never on a standalone app page, in a chat, or to a shared-browser guest).
 * It is drawn only while the owner can see it: not while the page is hidden,
 * nor while something else is over the app (``covered``: the app's loading
 * cover, one of the canvas's own dialogs, or the drawer). Shown again, it is
 * a new card whose answers wait again, so a tap meant for what was there
 * never answers it. The list is also read when the owner returns to this
 * browser window, so no polling is needed.
 */
export default function AppKeyRequestCard({ appId, covered = false }) {
  const owner = !isSharedBrowserAuth()
  const keysQuery = useKeys({ enabled: owner, refetchOnWindowFocus: true })
  const { names } = useAppNames()
  const queryClient = useQueryClient()
  const seen = useSyncExternalStore(subscribeToPageVisibility, pageVisible, pageVisible)
  if (!owner || covered || !seen) return null
  const requests = appKeyRequestsToShow(keysQuery.data, appId)
  if (requests.length === 0) return null
  const [request] = requests
  const more = requests.length - 1

  return (
    <div className="app-key-request">
      <section className="app-key-request__card">
        <KeyRequest
          key={requestFormKey(request)}
          // Only an app's request can be for a switched-off app; this is the one place it is asked.
          request={{ ...request, app_paused: requestAppPaused(keysQuery.data?.keys, request) }}
          names={names}
          onAnswered={() => queryClient.invalidateQueries({ queryKey: KEYS_QUERY })}
          dialog
        >
          {more > 0 && (
            <p className="app-key-request__note">
              {more === 1 ? 'One more request follows.' : `${more} more requests follow.`}
            </p>
          )}
        </KeyRequest>
      </section>
    </div>
  )
}
