/* The permission request shown the first time a Store app is opened: what it
   asks for, and one answer. It renders in the app's own pane (where the other
   app-open status panels appear), so it works in the shell, the standalone app
   and split panes, and never inside the app's sandboxed frame. All or nothing:
   Allow access or Not now. */
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import AppIcon from '../AppIcon.jsx'
import AppConsentAgents from './AppConsentAgents.jsx'
import './AppConsentPanel.css'

export default function AppConsentPanel({ appId, appName, request }) {
  const queryClient = useQueryClient()
  const { data: apps } = appQueries.list.useQuery()
  const app = (apps || []).find(item => String(item.id) === String(appId))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [declined, setDeclined] = useState(false)
  const name = request?.app_name || appName || 'This app'
  const rows = request?.rows || []
  // Agent access is one block; the app's other permissions stay rows.
  const agentRow = rows.find(row => row.group === 'agent')
  const otherRows = rows.filter(row => row.group !== 'agent')
  const isUpdate = Boolean(request?.is_update)
  const icon = <AppIcon className="app-consent__icon" item={app || { slug: appId }} label={name} size={null} />

  async function allow() {
    setBusy(true)
    setError('')
    try {
      const res = await api.apps.acceptConsent(appId, request.digest)
      if (res.status === 409) {
        // The app changed after this was shown: ask again with the new request.
        await appQueries.consent.invalidate(queryClient, appId)
        setError('The app’s request changed. Review it again.')
        return
      }
      if (!res.ok) throw new Error(String(res.status))
      await appQueries.consent.invalidate(queryClient, appId)
      await appQueries.token.invalidate(queryClient, appId)
      await appQueries.list.invalidate(queryClient)
    } catch {
      setError('Your choice could not be saved. No changes were made.')
    } finally {
      setBusy(false)
    }
  }

  if (declined) {
    return <div className="canvas-wrap app-consent-wrap"><section className="app-consent app-consent--closed" aria-label={`${name} is not allowed`}>
      {icon}
      <h2 className="app-consent__title">Access not granted</h2>
      <p className="app-consent__detail">{name} stays closed until you review and allow the access it requests. No changes were made.</p>
      <div className="app-consent__actions">
        <button type="button" className="app-consent__btn app-consent__btn--primary" onClick={() => setDeclined(false)}>Review access</button>
      </div>
    </section></div>
  }

  return <div className="canvas-wrap app-consent-wrap"><section className="app-consent" aria-label={`${name} requests access`}>
    <header className="app-consent__head">
      {icon}
      <div>
        <p className="app-consent__eyebrow">{isUpdate ? 'Updated permissions' : 'Permission request'}</p>
        <h2 className="app-consent__title">{name} requests access</h2>
      </div>
    </header>
    {request?.app_description && !isUpdate && <p className="app-consent__detail">{request.app_description}</p>}
    <div className="app-consent__section">
      <p className="app-consent__label">{isUpdate ? 'Newly requested' : 'Access requested'}</p>
      {agentRow && <AppConsentAgents row={agentRow} />}
      {otherRows.length > 0 && <ul className="app-consent__list" tabIndex={0} aria-label={`Access ${name} requests`}>
        {otherRows.map(row => <li className="app-consent__row" key={row.id}>
          <div>
            <strong>{row.title}{row.level ? ` (${row.level})` : ''}</strong>
            <span>{row.text}</span>
            {row.note && <span className="app-consent__note">{row.note}</span>}
          </div>
          {row.critical && <span className="app-consent__dot" role="img" aria-label="Critical: can change your Möbius" title="Critical: can change your Möbius" />}
        </li>)}
      </ul>}
    </div>
    <p className="app-consent__meta">{request?.baseline}</p>
    {error && <p className="app-consent__error" role="alert">{error}</p>}
    <div className="app-consent__actions">
      <button type="button" className="app-consent__btn" onClick={() => setDeclined(true)} disabled={busy}>Not now</button>
      <button type="button" className="app-consent__btn app-consent__btn--primary" onClick={allow} disabled={busy}>{busy ? 'Saving…' : 'Allow access'}</button>
    </div>
  </section></div>
}
