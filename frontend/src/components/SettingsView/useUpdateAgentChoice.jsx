/** A per-job agent choice; closing the picker never starts or changes a chat. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { authQueries, modelQueries } from '../../hooks/queries.js'
import { resolveProviderAvailability } from '../../lib/providerAvailability.js'
import { orderPickerModels } from '../../lib/modelPickerOrder.js'
import { providerInfoFor, providerOrderFor } from '../ChatView/providerRegistry.jsx'
import ModelSheet from '../ui/ModelSheet.jsx'
import { modelEfforts, validEffort } from '../ui/modelEfforts.js'

export function UpdateAgentPicker({ onChoice }) {
  const registryQuery = modelQueries.registry.useQuery()
  const prefsQuery = modelQueries.prefs.useQuery()
  const statusQuery = authQueries.provider.statuses.useQuery()
  const [draft, setDraft] = useState(null)
  const registry = registryQuery.data || {}
  const availability = resolveProviderAvailability(statusQuery)
  const saved = prefsQuery.data?.selected_agent
  const selected = draft || {
    provider: saved?.provider || '',
    model: saved?.model || '',
    effort: saved?.effort || '',
  }
  const rows = providerOrderFor(registry).flatMap(provider => {
    if (!availability.configuredProviders.has(provider)) return []
    const info = providerInfoFor(provider, statusQuery.data)
    const models = (registry[provider] || [])
      .filter(model => !prefsQuery.data?.hidden_ids?.includes(model.id)
        || (selected.provider === provider && selected.model === model.id))
      .map(model => ({ ...model, name: model.label || model.id }))
    return models.map(model => ({ provider, model, info }))
  })
  // Each consecutive provider run is a group, retaining global MRU order.
  const groups = []
  for (const row of orderPickerModels(rows, prefsQuery.data?.recent_ids)) {
    const last = groups.at(-1)
    if (last?.key === row.provider) last.models.push(row.model)
    else groups.push({ key: row.provider, label: row.info.label, Logo: row.info.Logo, models: [row.model] })
  }
  const entry = rows.find(row => row.provider === selected.provider && row.model.id === selected.model)?.model
  const efforts = modelEfforts(providerInfoFor(selected.provider, statusQuery.data).efforts, entry)
  const failed = registryQuery.isError || prefsQuery.isError || prefsQuery.data?.unavailable || statusQuery.isError
  const loading = registryQuery.isPending || prefsQuery.isPending || prefsQuery.isFetching || statusQuery.isPending
  const notice = failed ? 'Could not load your agents. Close this picker and try again.'
    : loading ? 'Loading your agents…'
      : 'Uses your latest chat agent. Completed jobs are kept in Archived.'
  return <ModelSheet open collapsible title="Update agent" groups={groups}
    provider={selected.provider} model={selected.model}
    configuredProviders={availability.configuredProviders}
    onClose={() => onChoice(null)} notice={notice}
    efforts={efforts} effort={validEffort(efforts, selected.effort)}
    onEffortChange={effort => setDraft({ ...selected, effort })}
    onPick={(provider, model, row) => {
      const nextEfforts = modelEfforts(providerInfoFor(provider, statusQuery.data).efforts, row)
      setDraft({ provider, model, effort: validEffort(nextEfforts,
        provider === selected.provider ? selected.effort : undefined) })
    }}
    confirmLabel="Start update agent" confirmDisabled={!entry || failed || loading}
    onConfirm={() => onChoice({ provider: selected.provider, model: selected.model,
      ...(efforts.length ? { effort: validEffort(efforts, selected.effort) } : {}) })} />
}

export default function useUpdateAgentChoice() {
  const [open, setOpen] = useState(false)
  const pending = useRef(null)
  const chooseAgent = useCallback(() => {
    if (pending.current) return pending.current.promise
    let resolve
    const promise = new Promise(done => { resolve = done })
    pending.current = { promise, resolve }
    setOpen(true)
    return promise
  }, [])
  const settle = useCallback(choice => {
    const request = pending.current
    pending.current = null
    setOpen(false)
    request?.resolve(choice)
  }, [])
  useEffect(() => () => {
    pending.current?.resolve(null)
    pending.current = null
  }, [])
  return { chooseAgent, agentPicker: open ? <UpdateAgentPicker onChoice={settle} /> : null }
}
