/* One provider's chat models on its Settings page, each with a switch for whether it appears in the chat picker; changes save as you toggle. */
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Switch } from '@openai/apps-sdk-ui/components/Switch'
import { modelQueries } from '../../hooks/queries.js'

/**
 * Picker visibility is one owner-wide list of hidden model ids. Each switch
 * shows its change at once and saves only that model
 * (modelQueries.prefs.saveChanges), so a switch here can never overwrite a
 * change made on another page. Rendered only for a connected provider. When
 * the saved list could not be read, the switches stay off-limits rather than
 * showing a state that is not the owner's.
 */
export default function ChatModelsList({ provider }) {
  const queryClient = useQueryClient()
  const registryQuery = modelQueries.registry.useQuery()
  const prefsQuery = modelQueries.prefs.useQuery()
  const [error, setError] = useState('')
  const models = Array.isArray(registryQuery.data?.[provider]) ? registryQuery.data[provider] : []
  const hidden = new Set(prefsQuery.data?.hidden_ids || [])
  const unavailable = !!prefsQuery.data?.unavailable

  const setVisible = async (modelId, visible) => {
    setError('')
    const saved = await modelQueries.prefs.saveChanges(queryClient, { [modelId]: !visible })
    setError(saved ? '' : 'Could not save that change. Try again.')
  }

  if (!models.length) {
    return (
      <div className="settings-item">
        <span className="settings-item__body">
          <span className="settings-item__sub">
            {registryQuery.isPending || prefsQuery.isPending ? 'Loading models…' : 'No chat models are available right now.'}
          </span>
        </span>
      </div>
    )
  }

  const message = unavailable ? 'Could not load which models are shown. Try again later.' : error
  return (
    <>
      {models.map((model) => {
        const visible = !hidden.has(model.id)
        const label = model.label || model.id
        const switchId = `chat-model-${provider}-${model.id}`.replace(/[^A-Za-z0-9_-]/g, '-')
        return (
          <div className="settings-item" key={model.id}>
            <span className="settings-item__body">
              <label className="settings-item__title" htmlFor={switchId}>{label}</label>
              {provider !== 'mobius' && label !== model.id && <span className="settings-item__sub">{model.id}</span>}
            </span>
            <span className="settings-item__trail">
              <Switch
                className="settings-switch"
                id={switchId}
                checked={visible}
                disabled={unavailable || prefsQuery.isPending}
                onCheckedChange={(next) => setVisible(model.id, next)}
              />
            </span>
          </div>
        )
      })}
      {message && <div className="settings-item-panel"><p className="settings-message settings-message--error" role="alert">{message}</p></div>}
    </>
  )
}
