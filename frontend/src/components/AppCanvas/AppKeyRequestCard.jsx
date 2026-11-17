/* The key request of the app in this window, asked in the moment: a dialog in the middle of the app while the owner has it open. */
import { useQueryClient } from '@tanstack/react-query'
import { isSharedBrowserAuth } from '../../api/client.js'
import KeyRequest from '../SettingsView/KeyRequest.jsx'
import { KEYS_QUERY, useAppNames, useKeys } from '../SettingsView/keysQueries.js'
import { requestAppPaused, requestFormKey } from '../../lib/keysSettingsText.js'
import { appKeyRequestsToShow } from '../../lib/keyRequestPlaces.js'
import '../SettingsView/KeyRequestPlace.css'
import './AppKeyRequestCard.css'

/**
 * An app's key requests are asked only while the owner is using it: no
 * notification, nothing waiting in Settings. A request the app makes while
 * open shows straight away (the server nudges the Shell, which re-reads the
 * requests); one made while it was closed shows when the owner opens it.
 * Not now is the only way out, so the question is always answered.
 *
 * Shown only in the owner's own shell, over a visible app (never on a
 * standalone app page or to a shared-browser guest). The list is also read
 * when the owner returns to this browser window, so no polling is needed.
 */
export default function AppKeyRequestCard({ appId, visible }) {
  const owner = !isSharedBrowserAuth()
  const keysQuery = useKeys({ enabled: owner && visible, refetchOnWindowFocus: true })
  const { names } = useAppNames()
  const queryClient = useQueryClient()
  if (!owner || !visible) return null
  const requests = appKeyRequestsToShow(keysQuery.data, appId)
  if (requests.length === 0) return null
  const [request] = requests
  const more = requests.length - 1

  return (
    <div className="app-key-request">
      <section className="key-request-place app-key-request__card" role="dialog" aria-modal="true" aria-label="Key request">
        <KeyRequest
          key={requestFormKey(request)}
          request={{ ...request, app_paused: requestAppPaused(keysQuery.data?.keys, request) }}
          names={names}
          onAnswered={() => queryClient.invalidateQueries({ queryKey: KEYS_QUERY })}
        />
        {more > 0 && (
          <p className="key-request-place__note">
            {more === 1 ? 'One more request follows.' : `${more} more requests follow.`}
          </p>
        )}
      </section>
    </div>
  )
}
