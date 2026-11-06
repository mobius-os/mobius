/* One AI provider's Settings page: usage, the background-task model, chat models and the connection, each as its own section. */
import { useState } from 'react'
import ModelSheet from '../ui/ModelSheet.jsx'
import ChatModelsList from './ChatModelsList.jsx'
import { modelEfforts, validEffort } from '../ui/modelEfforts.js'

function BackgroundModel({ provider, info, models, configured, enabled, model, effort, onModelChange, onEffortChange }) {
  const [sheetOpen, setSheetOpen] = useState(false)
  const Logo = info?.Logo
  const label = info?.label || provider
  const selectedModel = enabled ? model : ''
  const selectedRow = models.find((m) => m.id === selectedModel)
  const efforts = info?.efforts || []
  const selectedEfforts = modelEfforts(efforts, selectedRow)
  const effortLabel = enabled
    ? (selectedEfforts.find((e) => e.value === effort)?.label || '')
    : ''
  const selectedEffortIndex = Math.max(
    0,
    selectedEfforts.findIndex((candidate) => candidate.value === effort),
  )
  const triggerLabel = enabled
    ? (selectedRow?.label || selectedModel || 'Choose model')
    : 'None'
  // This page is one fixed provider, so the sheet lists only its models plus
  // "None", which takes the provider out of background work. Effort lives in
  // the sheet under the chosen model because each model has its own scale.
  const groups = [{
    key: provider,
    label,
    Logo,
    models: configured ? models : [],
  }]

  return (
    <>
      <button
        type="button"
        className={`settings-item settings-model-row${enabled ? '' : ' settings-model-row--off'}`}
        title={selectedModel || triggerLabel}
        onClick={() => setSheetOpen(true)}
        disabled={!configured}
        aria-haspopup="dialog"
        aria-label={`${label} background model: ${triggerLabel}${effortLabel ? `, ${effortLabel} effort` : ''}`}
      >
        <span className="settings-item__body">
          <span className="settings-item__title">{triggerLabel}</span>
          <span className="settings-item__sub">
            {enabled
              ? [effortLabel && `${effortLabel} effort`, selectedModel].filter(Boolean).join(' · ')
              : 'Not used for background tasks'}
          </span>
        </span>
        {enabled && effortLabel && (
          <span className="settings-bg-row__effort-visual" aria-hidden="true">
            {selectedEfforts.map((candidate, index) => (
              <span
                key={candidate.value}
                className={
                  'settings-bg-row__effort-dot'
                  + (index <= selectedEffortIndex ? ' settings-bg-row__effort-dot--filled' : '')
                  + (index === selectedEffortIndex ? ' settings-bg-row__effort-dot--on' : '')
                }
              />
            ))}
          </span>
        )}
      </button>
      <ModelSheet
        open={sheetOpen}
        onClose={() => setSheetOpen(false)}
        title={`${label} model`}
        groups={groups}
        provider={enabled ? provider : ''}
        model={selectedModel}
        efforts={efforts}
        effort={effort}
        configuredProviders={new Set(configured ? [provider] : [])}
        onEffortChange={onEffortChange}
        onPick={(pid, id, pickedModel) => {
          const nextEfforts = modelEfforts(efforts, pickedModel)
          onModelChange(id, validEffort(nextEfforts, effort))
        }}
        allowNone
        noneLabel="None (disable)"
        onNone={() => onModelChange('')}
      />
    </>
  )
}

/**
 * Layout for one provider page. The parent owns data and side effects and
 * passes ready sections: `usage` (the Usage section), `access` (provider-
 * specific sections such as Möbius model access) and `connection` (the
 * connect/reconnect/disconnect panel).
 */
export default function ProviderPage({
  provider,
  info,
  models,
  configured,
  enabled,
  model,
  effort,
  onModelChange,
  onEffortChange,
  usage = null,
  access = null,
  connection = null,
  connectionDetails = [],
}) {
  const connectionSection = connection && (
    <section className="settings-group" aria-labelledby={`settings-${provider}-connection`}>
      <div className="settings-group__head">
        <div className="settings-group__heading">
          <h2 className="settings-group__title" id={`settings-${provider}-connection`}>Connection</h2>
          {!configured && <p className="settings-group__desc">Connect {info?.label || provider} to use it for chats and background tasks.</p>}
        </div>
      </div>
      <div className="settings-list">
        {connectionDetails.map(({ label, value }) => (
          <div className="settings-item" key={label}>
            <span className="settings-item__body"><span className="settings-item__title">{label}</span></span>
            <span className="settings-item__value">{value}</span>
          </div>
        ))}
        <div className="settings-item settings-item--block settings-provider-connection">{connection}</div>
      </div>
    </section>
  )

  // Until it is connected, a provider's models and settings cannot be used,
  // so its page is only the way to connect it.
  if (!configured) return <>{connectionSection}{access}</>

  return (
    <>
      {usage}
      {access}
      <section className="settings-group" aria-labelledby={`settings-${provider}-background`}>
        <div className="settings-group__head">
          <div className="settings-group__heading">
            <h2 className="settings-group__title" id={`settings-${provider}-background`}>Background tasks</h2>
            <p className="settings-group__desc">The model used when background work runs on {info?.label || provider}.</p>
          </div>
        </div>
        <div className="settings-list">
          <BackgroundModel
            provider={provider}
            info={info}
            models={models}
            configured={configured}
            enabled={enabled}
            model={model}
            effort={effort}
            onModelChange={onModelChange}
            onEffortChange={onEffortChange}
          />
        </div>
      </section>
      <section className="settings-group" aria-labelledby={`settings-${provider}-chat-models`}>
        <div className="settings-group__head">
          <div className="settings-group__heading">
            <h2 className="settings-group__title" id={`settings-${provider}-chat-models`}>Chat models</h2>
            <p className="settings-group__desc">Switched-on models appear in the chat model picker.</p>
          </div>
        </div>
        <div className="settings-list">
          <ChatModelsList provider={provider} configured={configured} />
        </div>
      </section>
      {connectionSection}
    </>
  )
}
