/* The key request of the app in this window, as a card over it: a sheet along the bottom of a narrow window, a floating card on a wide one. */
import { useReducer } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Key } from '@openai/apps-sdk-ui/components/Icon'
import { isSharedBrowserAuth } from '../../api/client.js'
import KeyRequest from '../SettingsView/KeyRequest.jsx'
import { KEYS_QUERY, useAppNames, useKeys } from '../SettingsView/keysQueries.js'
import { requestAppPaused, requestFormKey } from '../../lib/keysSettingsText.js'
import { appKeyRequestsToShow } from '../../lib/keyRequestPlaces.js'
import '../SettingsView/KeyRequestPlace.css'
import './AppKeyRequestCard.css'

// Requests the owner put off with Later. Module state, so closing and
// reopening the app keeps them put off until the page reloads.
const putOff = new Set()

/**
 * Shown only in the owner's own shell, over a visible app (never on a
 * standalone app page or to a shared-browser guest), and only around the
 * card itself, so the app stays usable beside it. Settings › Keys lists the
 * same requests.
 *
 * The list is read when the app becomes visible and when the owner returns
 * to this browser window; the Shell refreshes it on every new notification,
 * and a new request always notifies, so no polling is needed.
 */
export default function AppKeyRequestCard({ appId, visible }) {
  const owner = !isSharedBrowserAuth()
  const keysQuery = useKeys({ enabled: owner && visible, refetchOnWindowFocus: true })
  const { names } = useAppNames()
  const queryClient = useQueryClient()
  const [, putOffChanged] = useReducer(count => count + 1, 0)
  if (!owner || !visible) return null
  const requests = appKeyRequestsToShow(keysQuery.data, appId, putOff)
  if (requests.length === 0) return null
  const [request] = requests
  const more = requests.length - 1

  function later() {
    for (const shown of requests) putOff.add(requestFormKey(shown))
    putOffChanged()
  }

  return (
    <div className="app-key-request">
      <div className="app-key-request__dock">
        <section className="key-request-place app-key-request__card" aria-label="Key request">
          <div className="app-key-request__head">
            <span className="app-key-request__title">
              <Key width={16} height={16} aria-hidden="true" /> Key request
            </span>
            <button type="button" className="key-request-place__link app-key-request__later" onClick={later}>
              Later
            </button>
          </div>
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
    </div>
  )
}
