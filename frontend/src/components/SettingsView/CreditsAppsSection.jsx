/**
 * Settings → Credits: the local apps the owner allowed to use their credits.
 *
 * A local app asks the first time it pays (AppCanvas's credits question);
 * this is where that answer stays visible and can be taken back. Store apps
 * are not listed: their first-open consent is managed with the app.
 */

import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, apiFetch, jsonOrThrow } from '../../api/client.js'

const KEY = ['credits-apps']

export default function CreditsAppsSection() {
  const queryClient = useQueryClient()
  const [removing, setRemoving] = useState(null)
  const [refusal, setRefusal] = useState('')
  const { data } = useQuery({
    queryKey: KEY,
    queryFn: async () => jsonOrThrow(await apiFetch('/identity/credits/apps')),
    staleTime: 30_000,
  })
  const apps = data?.apps || []
  if (!apps.length) return null

  async function remove(app) {
    setRemoving(app.app_id)
    setRefusal('')
    try {
      const res = await api.apps.revokeCredits(app.app_id)
      if (!res.ok) setRefusal(`${app.name} could not be removed. Try again.`)
    } catch {
      setRefusal(`${app.name} could not be removed. Try again.`)
    } finally {
      setRemoving(null)
      void queryClient.invalidateQueries({ queryKey: KEY })
    }
  }

  return (
    <section className="settings__section credits" aria-labelledby="credits-apps-title">
      <h2 className="settings__section-title" id="credits-apps-title">Apps that can use your credits</h2>
      <p className="credits__quiet">They ask the first time they pay. Remove one and it asks again.</p>
      <ul className="credits__apps">
        {apps.map(app => (
          <li key={app.app_id} className="credits__app">
            <span className="credits__app-name">{app.name}</span>
            <span className="credits__app-what">{app.rows.map(row => row.title).join(' · ')}</span>
            <button type="button" className="settings__btn settings__btn--outline settings__btn--sm"
              onClick={() => remove(app)} disabled={removing === app.app_id}>
              {removing === app.app_id ? 'Removing…' : 'Remove'}
            </button>
          </li>
        ))}
      </ul>
      {refusal && <p className="credits__refusal" role="alert">{refusal}</p>}
    </section>
  )
}
