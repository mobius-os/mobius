import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import useExpiryCountdowns from '../../hooks/useExpiryCountdowns.js'
import AppIcon from '../AppIcon.jsx'
import { failureMessage, removedLabel } from './uninstalledApps.js'
import './UninstalledApps.css'

const ACTIONS = {
  recover: { call: id => api.apps.recover(id), working: 'Recovering…', failed: 'Could not recover this app. Try again.' },
  delete: { call: id => api.apps.deletePermanently(id), working: 'Deleting…', failed: 'Could not delete this app. Try again.' },
}

// One uninstalled app. It tracks its own work, so acting on one tile never locks the others.
function UninstalledTile({ row, countdown, onError }) {
  const queryClient = useQueryClient()
  const [working, setWorking] = useState(null)

  async function run(kind) {
    setWorking(kind)
    onError('')
    try {
      const response = await ACTIONS[kind].call(row.id)
      if (!response.ok) {
        onError(await failureMessage(response, ACTIONS[kind].failed))
        return
      }
      await Promise.all([
        appQueries.list.invalidate(queryClient),
        appQueries.uninstalled.invalidate(queryClient),
      ])
    } catch {
      onError('Could not reach Möbius. Try again.')
    } finally {
      setWorking(null)
    }
  }

  return (
    <li className="uninstalled-apps__tile">
      <AppIcon
        item={{
          id: row.id,
          slug: row.slug,
          icon_url: row.icon_data_url,
          background_color: row.background_color,
          theme_color: row.theme_color,
        }}
        label={row.name}
        size={null}
        className="apps-directory__card-icon"
      />
      <span className="apps-directory__card-name uninstalled-apps__name">{row.name}</span>
      <span className="uninstalled-apps__removed">{removedLabel(row.deleted_at)}</span>
      <span className={`uninstalled-apps__expiry${countdown.tone ? ` is-${countdown.tone}` : ''}`}>
        {countdown.label}
      </span>
      <div className="uninstalled-apps__actions">
        <button
          type="button"
          className="uninstalled-apps__btn uninstalled-apps__btn--primary"
          disabled={working !== null}
          onClick={() => run('recover')}
          aria-label={`Recover ${row.name}`}
        >
          {working === 'recover' ? ACTIONS.recover.working : 'Recover'}
        </button>
        <button
          type="button"
          className="uninstalled-apps__btn uninstalled-apps__btn--danger"
          disabled={working !== null}
          onClick={() => run('delete')}
          aria-label={`Delete ${row.name} permanently`}
        >
          {working === 'delete' ? ACTIONS.delete.working : 'Delete permanently'}
        </button>
      </div>
    </li>
  )
}

// Apps that were uninstalled but are still in their recovery window. Hidden
// when there are none, so it costs nothing until it is needed.
export default function UninstalledApps() {
  const { data: rows = [], dataUpdatedAt } = appQueries.uninstalled.useQuery()
  const [error, setError] = useState('')
  const live = useExpiryCountdowns(rows, dataUpdatedAt)

  if (live.length === 0) return null

  return (
    <section className="uninstalled-apps" aria-labelledby="uninstalled-apps-title">
      <div className="uninstalled-apps__head">
        <h2 id="uninstalled-apps-title">
          Recently uninstalled <span className="uninstalled-apps__count">{live.length}</span>
        </h2>
        <p>
          You can recover an uninstalled app, with its data, for 7 days. After that it is
          removed permanently.
        </p>
      </div>
      <ul className="uninstalled-apps__list">
        {live.map(({ row, countdown }) => (
          <UninstalledTile key={row.id} row={row} countdown={countdown} onError={setError} />
        ))}
      </ul>
      {error && <p className="uninstalled-apps__error" role="alert">{error}</p>}
    </section>
  )
}
