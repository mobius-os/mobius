/* Local connection management keeps reconnect and confirmed sign-out together. */
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { authQueries, modelQueries, settingsQueries } from '../../hooks/queries.js'
import { detailToMessage } from '../../lib/errorDetail.js'

/** Local sign-out for one provider, with the queries every picker reads refreshed. */
export function useProviderDisconnect(provider, onDisconnected) {
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function disconnect() {
    setBusy(true)
    setError('')
    try {
      const response = await api.auth.provider.disconnect(provider)
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        setError(detailToMessage(data.detail, 'Could not disconnect. Please try again.'))
        return false
      }
      const statuses = authQueries.provider.statuses
      await queryClient.cancelQueries({ queryKey: statuses.key })
      queryClient.setQueryData(statuses.key, current => ({
        ...current,
        [provider]: { ...current?.[provider], configured: false, error: null },
      }))
      void statuses.invalidate(queryClient)
      void modelQueries.registry.invalidate(queryClient)
      void settingsQueries.providerUsage.invalidate(queryClient, provider)
      onDisconnected?.()
      return true
    } catch {
      setError('Could not confirm the connection status. Please try again.')
      return false
    } finally {
      setBusy(false)
    }
  }

  return { disconnect, busy, error, setError }
}

export default function ProviderConnection({ provider, name, connected, onDisconnected, children }) {
  const [action, setAction] = useState('manage')
  const { disconnect, busy, error, setError } = useProviderDisconnect(provider, onDisconnected)

  if (!connected || action === 'reconnect') return children

  return (
    <div className="provider-connection" aria-busy={busy}>
      {action === 'disconnect' ? (
        <>
          <p>Disconnect {name} from this Möbius?</p>
          <p className="pa__muted">
            Your chats and settings stay. New tasks using this provider will need
            a connection. Running tasks may keep their session until they finish.
            This does not cancel your subscription or sign out other devices.
          </p>
          <div className="provider-connection__actions">
            <button type="button" className="settings__btn settings__btn--sm settings__btn--outline" disabled={busy} onClick={() => { setAction('manage'); setError('') }}>Cancel</button>
            <button type="button" className="settings__btn settings__btn--sm settings__btn--danger" disabled={busy} onClick={disconnect}>{busy ? 'Disconnecting…' : 'Disconnect'}</button>
          </div>
        </>
      ) : (
        <div className="provider-connection__actions">
          <button type="button" className="settings__btn settings__btn--sm settings__btn--outline" onClick={() => setAction('reconnect')}>Reconnect</button>
          <button type="button" className="settings__btn settings__btn--sm settings__btn--danger" onClick={() => setAction('disconnect')}>Disconnect…</button>
        </div>
      )}
      {error && <p className="pa__error" role="alert">{error}</p>}
    </div>
  )
}
