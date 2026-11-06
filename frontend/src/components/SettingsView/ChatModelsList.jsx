/* One provider's chat models on its Settings page, each with a switch for whether it appears in the chat picker; changes save as you toggle. */
import { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Switch } from '@openai/apps-sdk-ui/components/Switch'
import { api } from '../../api/client.js'
import { modelQueries } from '../../hooks/queries.js'
import { createLatestSaveQueue } from '../../lib/modelPrefsSaveQueue.js'

/**
 * Picker visibility is one owner-wide list of hidden model ids
 * (PATCH /api/owner/model-prefs). Toggles show at once; createLatestSaveQueue
 * sends them one save at a time with the latest list, and restores the
 * server's confirmed list when a save fails with nothing newer pending.
 */
export default function ChatModelsList({ provider, configured }) {
  const queryClient = useQueryClient()
  const registryQuery = modelQueries.registry.useQuery()
  const prefsQuery = modelQueries.prefs.useQuery()
  const [error, setError] = useState('')
  const queueRef = useRef(null)
  if (!queueRef.current) {
    queueRef.current = createLatestSaveQueue({
      read: () => [...(queryClient.getQueryData(modelQueries.prefs.key)?.hidden_ids || [])],
      show: hiddenIds => queryClient.setQueryData(modelQueries.prefs.key, { hidden_ids: hiddenIds }),
      save: async hiddenIds => (await api.owner.modelPrefs.save(hiddenIds)).ok,
      onResult: ok => setError(ok ? '' : 'Could not save that change. Try again.'),
    })
  }
  const models = Array.isArray(registryQuery.data?.[provider]) ? registryQuery.data[provider] : []
  const hidden = new Set(prefsQuery.data?.hidden_ids || [])

  const setVisible = (modelId, visible) => {
    void queueRef.current.change((hiddenIds) => {
      const next = new Set(hiddenIds)
      if (visible) next.delete(modelId)
      else next.add(modelId)
      return [...next]
    })
  }

  if (!models.length) {
    return (
      <div className="settings-item">
        <span className="settings-item__body">
          <span className="settings-item__sub">
            {registryQuery.isPending || prefsQuery.isPending
              ? 'Loading models…'
              : configured ? 'No chat models are available right now.' : 'Connect to see its chat models.'}
          </span>
        </span>
      </div>
    )
  }

  return (
    <>
      {models.map((model) => {
        const visible = !hidden.has(model.id)
        const label = model.label || model.id
        const switchId = `chat-model-${provider}-${model.id}`.replace(/[^A-Za-z0-9_-]/g, '-')
        return (
          <div className={`settings-item chat-model-row${visible ? '' : ' chat-model-row--hidden'}`} key={model.id}>
            <span className="settings-item__body">
              <label className="settings-item__title" htmlFor={switchId}>{label}</label>
              {provider !== 'mobius' && label !== model.id && <span className="settings-item__sub">{model.id}</span>}
            </span>
            <span className="settings-item__trail">
              <Switch
                className="settings-switch"
                id={switchId}
                checked={visible}
                disabled={!configured || prefsQuery.isPending}
                onCheckedChange={(next) => setVisible(model.id, next)}
              />
            </span>
          </div>
        )
      })}
      {error && <div className="settings-item-panel"><p className="settings-message settings-message--error" role="alert">{error}</p></div>}
    </>
  )
}
