/**
 * ChatSettingsPanel — the per-chat model + effort picker inside the
 * composer's `+` popover. Renders the model rows and effort slider, and owns
 * the confirmation and atomic handoff flow used for cross-provider switches
 * after a chat has assistant turns.
 *
 * ╔══════════════════════════════════════════════════════════════════╗
 * ║                                                                  ║
 * ║   FOUR LOAD-BEARING CONTRACTS — read before refactoring          ║
 * ║                                                                  ║
 * ║   1. PER-PROVIDER EFFORT MEMORY                                  ║
 * ║      Provider effort enums DO NOT MAP across one another.        ║
 * ║      Codex `medium` is roughly Claude `low`. Each provider       ║
 * ║      remembers its OWN last-picked effort via                    ║
 * ║      `effort_by_provider: { codex, claude, mobius }` in          ║
 * ║      `agent_settings`.                                           ║
 * ║      Switching providers swaps the active `effort` to that       ║
 * ║      provider's last value (fallback: current effort).           ║
 * ║      Picking effort writes BOTH `effort` (active) and the        ║
 * ║      updated `effort_by_provider` map in one PATCH.              ║
 * ║                                                                  ║
 * ║      Enum reference:                                             ║
 * ║        Codex (6): none / minimal / low / medium / high / xhigh   ║
 * ║        Claude (6): low / medium / high / xhigh / max /           ║
 * ║                    ultracode                                     ║
 * ║        Möbius (5): minimal / low / medium / high / max           ║
 * ║      Runners forward the value as-is; an out-of-enum effort      ║
 * ║      surfaces as a 400 at turn time, not at PATCH (consistent    ║
 * ║      with the platform's "reversibility over prevention"         ║
 * ║      philosophy — see mobius/CLAUDE.md design philosophy).       ║
 * ║                                                                  ║
 * ║   2. CROSS-PROVIDER SWITCHING                                    ║
 * ║      Sessions are not portable between providers (Claude session ║
 * ║      id ≠ Codex thread id), so switching after assistant turns   ║
 * ║      asks for confirmation, then the INCOMING provider prepares  ║
 * ║      its context from the detailed running summary. The briefing,║
 * ║      provider, settings, and cleared session commit atomically.  ║
 * ║      Same-provider model swaps need no handoff.                   ║
 * ║      `hasAssistantTurns` is LIVE-DERIVED in the parent           ║
 * ║      (ChatView): `chatInfo.has_assistant_turns ||                ║
 * ║      messages.some(m => m.role === 'assistant')` — the           ║
 * ║      persisted flag isn't refreshed mid-turn, so the messages    ║
 * ║      check engages the lock the moment a reply lands.            ║
 * ║                                                                  ║
 * ║   3. ORDERED PICKER WRITES                                      ║
 * ║      `settingsSaveTailRef` is owned by ChatView, so model and    ║
 * ║      effort picks persist in tap order even if this popover is   ║
 * ║      closed. The same tail gates provider handoffs and message   ║
 * ║      sends. Rows stay interactive while routine saves settle.    ║
 * ║                                                                  ║
 * ║   4. KEYBOARD-STATE PRESERVATION                                 ║
 * ║      ComposerPopover owns one pointer handler on the popover.    ║
 * ║      Bubbling covers every present and future control            ║
 * ║      without leaking composer-specific focus props into shared   ║
 * ║      controls. Its `touch-action: pan-y` keeps the long model    ║
 * ║      list scrollable from any descendant.                        ║
 * ║                                                                  ║
 * ║   The shared <EffortStepper> renders a stepper track — NOT       ║
 * ║   pills, NOT a chip group. The slider was explicitly chosen      ║
 * ║   over chips by the user; an earlier proposed revert to chips    ║
 * ║   was rejected. Each provider's slider has its own length        ║
 * ║   (6 stops for Codex and Claude, 5 for Möbius). `findIndex`      ║
 * ║   defaults to                                                    ║
 * ║   0 when the persisted value isn't in the provider's enum, so    ║
 * ║   a cross-provider effort carryover renders gracefully.          ║
 * ║                                                                  ║
 * ║   Provider logos stay custom because the apps-sdk-ui icon set    ║
 * ║   ships UI glyphs, not vendor brand marks. Möbius reuses the     ║
 * ║   shell's Android notification silhouette as a CSS mask.         ║
 * ║                                                                  ║
 * ╚══════════════════════════════════════════════════════════════════╝
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { apiFetch } from '../../api/client.js'
import { Switch } from '@openai/apps-sdk-ui/components/Switch'
import { authQueries, modelQueries } from '../../hooks/queries.js'
import EffortStepper from '../ui/EffortStepper.jsx'
import { modelEfforts, validEffort } from '../ui/modelEfforts.js'
import {
  beginProviderSwitch,
  clearProviderSwitch,
  completeProviderSwitch,
  createProviderSwitchId,
  failProviderSwitch,
  isProviderSwitchBlocking,
  providerSwitchPayload,
  providerSwitchResponseData,
  restorableProviderSwitch,
  stageProviderSwitch,
} from './providerSwitch.js'
import {
  PROVIDER_AVAILABILITY_PHASE,
  resolveProviderAvailability,
  visibleProviderModels,
} from '../../lib/providerAvailability.js'
import { detailToMessage } from '../../lib/errorDetail.js'
import './ChatSettingsPanel.css'

/* Provider metadata + ordering live in their own light module so the picker,
 * Manage models, SettingsView, and background-agent defaults share one source
 * of truth. Re-exported here for existing importers. */
import { PROVIDER_INFO, PROVIDER_ORDER } from './providerRegistry.jsx'
export { PROVIDER_INFO, PROVIDER_ORDER }


/** Resolves the displayed model list for `providerId` from the live
 *  registry + owner prefs.
 *
 *  Rules (matches the codex-review spec):
 *    - Keep registry order. The backend returns the provider SDK/CLI
 *      order when live fetch succeeds, and fallback order otherwise.
 *    - Hide entries whose ID appears in `hiddenIds`, UNLESS that ID
 *      is the chat's currently-selected model (`selectedId`). The
 *      currently-selected model is always visible so the user can
 *      switch away from it.
 *    - Stale prefs are tolerated: an entry in `hiddenIds` that
 *      doesn't appear in the registry simply has no effect (we
 *      can't filter out something we can't see). No error.
 */
function resolveDisplayedModels(
  registryEntries, hiddenIds, selectedId,
) {
  if (!Array.isArray(registryEntries)) return []
  if (!hiddenIds || hiddenIds.length === 0) return registryEntries
  const hidden = new Set(hiddenIds)
  return registryEntries.filter(
    m => !hidden.has(m.id) || m.id === selectedId,
  )
}




export default function ChatSettingsPanel({
  chatId,
  chat,
  provider,
  effective,
  hasAssistantTurns,
  autoResumeEnabled = false,
  autoResumeSaving = false,
  autoResumeError = '',
  onAutoResumeChange,
  onChange,
  // Shared promise tail for picker writes, handoffs, and message sends.
  settingsSaveTailRef,
  // Per-chat external state survives the popover and ChatView itself. This is
  // what keeps navigation away/back from unlocking a live handoff or losing
  // its retry id and error feedback.
  providerSwitchState,
}) {
  const [saving, setSaving] = useState(false)
  const [localError, setLocalError] = useState('')
  const pendingSwitchPreviousRef = useRef(null)
  // Synchronous guard for the paid atomic handoff. Routine picker writes use
  // the serialized tail and deliberately remain available while saving.
  const providerSwitchInFlightRef = useRef(false)
  const pendingSwitch = restorableProviderSwitch(
    providerSwitchState?.request,
    chatId,
    provider || 'claude',
  )
  const compacting = providerSwitchState?.status === 'switching'
  const error = providerSwitchState?.error || localError

  // Wait for registry, preferences, and provider availability before exposing
  // rows. The registry deliberately contains fallback models for disconnected
  // providers, so rendering it before status settles leaks unusable choices.
  const registryQuery = modelQueries.registry.useQuery()
  const prefsQuery = modelQueries.prefs.useQuery()
  const providerStatusQuery = authQueries.provider.statuses.useQuery()
  const registry = registryQuery.data
  const prefs = prefsQuery.data
  const availability = resolveProviderAvailability(providerStatusQuery)
  const availabilitySettled = (
    availability.phase !== PROVIDER_AVAILABILITY_PHASE.LOADING
  )
  const dataReady = !!registry && !!prefs && availabilitySettled
  const loadingModels = !dataReady && (
    registryQuery.isLoading
    || registryQuery.isFetching
    || prefsQuery.isLoading
    || prefsQuery.isFetching
    || (!availabilitySettled && (
      providerStatusQuery.isLoading || providerStatusQuery.isFetching
    ))
  )
  const modelLoadError = !dataReady && (
    registryQuery.isError || prefsQuery.isError
  )

  const [draftModel, setDraftModel] = useState(effective?.model || '')
  const [draftEffort, setDraftEffort] = useState(effective?.effort || '')
  const [draftProvider, setDraftProvider] = useState(provider || 'claude')
  // Per-provider effort memory. The two SDKs' effort enums don't
  // map across providers (Codex `medium` ≈ Claude `low`), so each
  // provider remembers its OWN last-picked effort and we swap
  // `draftEffort` to that value when the user switches providers.
  // Initial value mirrors what the server sent under
  // `effective.effort_by_provider`; defaults to the current effort
  // bound to the current provider when the server hasn't recorded
  // any per-provider value yet.
  const [draftEffortByProvider, setDraftEffortByProvider] = useState(
    () => effective?.effort_by_provider || {},
  )

  useEffect(() => {
    // Successful writes are published to the parent in serialized server
    // order, but a newer optimistic choice may still be queued. Hold the
    // visible draft until the whole tail settles; the final success or failure
    // then reconciles from the last value the server actually accepted.
    if (saving) return
    setDraftModel(effective?.model || '')
    setDraftEffort(effective?.effort || '')
    setDraftEffortByProvider(effective?.effort_by_provider || {})
  }, [
    effective?.model,
    effective?.effort,
    effective?.effort_by_provider,
    chatId,
    saving,
  ])

  useEffect(() => {
    if (saving) return
    const sourceProvider = provider || 'claude'
    setDraftProvider(sourceProvider)
    const restored = restorableProviderSwitch(
      providerSwitchState?.request, chatId, sourceProvider,
    )
    if (
      providerSwitchState?.request
      && !restored
      && providerSwitchState?.status !== 'success'
    ) {
      clearProviderSwitch(chatId)
    }
    pendingSwitchPreviousRef.current = null
  }, [
    provider,
    chatId,
    providerSwitchState?.request,
    providerSwitchState?.status,
    saving,
  ])

  const patchChat = useCallback((body) => {
    if (!chatId) return Promise.resolve()
    setSaving(true)
    setLocalError('')
    const operation = settingsSaveTailRef.current.then(async () => {
      try {
        const res = await apiFetch(`/chats/${chatId}`, {
          method: 'PATCH',
          body: JSON.stringify(body),
        })
        if (!res.ok) return 'Could not save. Try again.'
        const data = await res.json()
        onChange?.({
          agent_settings_json: data.agent_settings_json,
          provider: data.provider,
          effective: data.effective,
        })
        return ''
      } catch {
        return 'Network error.'
      }
    })
    settingsSaveTailRef.current = operation
    operation.then(error => {
      if (settingsSaveTailRef.current !== operation) return
      setLocalError(error)
      setSaving(false)
    })
    return operation
  }, [chatId, onChange, settingsSaveTailRef])

  const switchProviderWithHandoff = useCallback(async ({
    provider: nextProvider,
    model,
    effort,
    effortByProvider,
    switchId,
  }) => {
    if (!chatId) return false
    const request = pendingSwitch || {
      chatId,
      sourceProvider: provider || 'claude',
      model,
      provider: nextProvider,
      efforts: modelEfforts(PROVIDER_INFO[nextProvider].efforts, { id: model }),
      switchId,
    }
    beginProviderSwitch(chatId, request)
    setLocalError('')
    try {
      // A same-provider choice made just before this confirmation is part of
      // the source chat state the handoff follows. Wait for that routine write
      // even if + was closed/reopened while it settled.
      await settingsSaveTailRef.current
      const res = await apiFetch(`/chats/${chatId}/provider-switch`, {
        method: 'POST',
        body: JSON.stringify(providerSwitchPayload({
          provider: nextProvider,
          model,
          effort,
          effortByProvider,
          switchId,
        })),
      })
      if (!res.ok) {
        // A validation failure (incompatible target model/effort) returns a
        // Pydantic `detail` ARRAY; normalize it to a string so it can never
        // reach `<p>{error}</p>` and crash the shell with React error #31.
        let detail = ''
        try { detail = detailToMessage((await res.json())?.detail) } catch {}
        failProviderSwitch(
          chatId,
          request,
          detail || 'Could not prepare this chat for the new provider.',
        )
        return false
      }
      const data = await providerSwitchResponseData(res, {
        provider: nextProvider,
        switchId,
      })
      if (!data) {
        failProviderSwitch(
          chatId,
          request,
          'The switch response was interrupted. Retry to confirm its state.',
        )
        return false
      }
      completeProviderSwitch(chatId, request, data)
      return true
    } catch {
      failProviderSwitch(
        chatId,
        request,
        'Network error while preparing the provider switch.',
      )
      return false
    }
  }, [chatId, pendingSwitch, provider, settingsSaveTailRef])

  const handleEffortChange = useCallback((value) => {
    // Remember this effort under the active provider so a later
    // provider-switch restores it; ship BOTH `effort` (active) and
    // the full `effort_by_provider` map so the server stores the
    // memory verbatim.
    const nextMap = { ...draftEffortByProvider, [draftProvider]: value }
    setDraftEffort(value)
    setDraftEffortByProvider(nextMap)
    patchChat({
      agent_settings_json: { effort: value, effort_by_provider: nextMap },
    })
  }, [draftProvider, draftEffortByProvider, patchChat])

  const switchProviderModel = useCallback(async (
    value, providerValue, allowedEfforts, switchId = createProviderSwitchId(),
  ) => {
    if (isProviderSwitchBlocking(chatId)) return false
    // Cross-provider switch: restore this provider's last-known effort and
    // normalize it against the selected model's declared scale.
    const nextEffort = validEffort(
      allowedEfforts,
      draftEffortByProvider[providerValue] ?? draftEffort,
    )
    const nextEffortByProvider = {
      ...draftEffortByProvider,
      [providerValue]: nextEffort,
    }

    if (!hasAssistantTurns) {
      setDraftModel(value)
      setDraftProvider(providerValue)
      setDraftEffort(nextEffort)
      setDraftEffortByProvider(nextEffortByProvider)
      patchChat({
        provider: providerValue,
        agent_settings_json: {
          model: value,
          effort: nextEffort,
          effort_by_provider: nextEffortByProvider,
        },
      })
      return true
    }

    if (providerSwitchInFlightRef.current) return false
    providerSwitchInFlightRef.current = true
    try {
      const ok = await switchProviderWithHandoff({
        provider: providerValue,
        model: value,
        effort: nextEffort,
        effortByProvider: nextEffortByProvider,
        switchId,
      })
      if (!ok) return false
      setDraftModel(value)
      setDraftProvider(providerValue)
      setDraftEffort(nextEffort)
      setDraftEffortByProvider(nextEffortByProvider)
      return true
    } finally {
      providerSwitchInFlightRef.current = false
    }
  }, [
    draftEffort,
    draftEffortByProvider,
    hasAssistantTurns,
    patchChat,
    switchProviderWithHandoff,
  ])

  const handlePickModel = useCallback(async (value, providerValue, allowedEfforts) => {
    if (providerValue !== draftProvider) {
      if (hasAssistantTurns) {
        if (providerSwitchInFlightRef.current) return
        if (!pendingSwitchPreviousRef.current) {
          pendingSwitchPreviousRef.current = {
            provider: draftProvider,
            model: draftModel,
            effort: draftEffort,
          }
        }
        setLocalError('')
        const request = {
          chatId,
          sourceProvider: provider || 'claude',
          model: value,
          provider: providerValue,
          efforts: allowedEfforts,
          switchId: createProviderSwitchId(),
        }
        stageProviderSwitch(chatId, request)
        return
      }
      await switchProviderModel(value, providerValue, allowedEfforts)
      return
    }
    clearProviderSwitch(chatId)
    // A same-provider pick abandons any pending cross-provider switch — clear the
    // captured prior selection too, so a later Cancel reverts to THIS choice, not
    // a stale earlier one (#7 ensemble finding).
    pendingSwitchPreviousRef.current = null
    const nextEffort = validEffort(allowedEfforts, draftEffort)
    const nextEffortByProvider = {
      ...draftEffortByProvider,
      [providerValue]: nextEffort,
    }
    setDraftModel(value)
    setDraftEffort(nextEffort)
    setDraftEffortByProvider(nextEffortByProvider)
    await patchChat({
      agent_settings_json: {
        model: value,
        effort: nextEffort,
        effort_by_provider: nextEffortByProvider,
      },
    })
  }, [
    draftProvider,
    draftModel,
    hasAssistantTurns,
    draftEffort,
    draftEffortByProvider,
    chatId,
    patchChat,
    provider,
    switchProviderModel,
  ])

  const handleConfirmProviderSwitch = useCallback(async () => {
    if (
      !pendingSwitch
      || compacting
      || providerSwitchInFlightRef.current
    ) return
    const ok = await switchProviderModel(
      pendingSwitch.model,
      pendingSwitch.provider,
      pendingSwitch.efforts,
      pendingSwitch.switchId,
    )
    if (ok) {
      pendingSwitchPreviousRef.current = null
    }
  }, [
    pendingSwitch,
    compacting,
    switchProviderModel,
  ])

  const handleCancelProviderSwitch = useCallback(() => {
    const previous = pendingSwitchPreviousRef.current
    if (previous) {
      setDraftProvider(previous.provider)
      setDraftModel(previous.model)
      setDraftEffort(previous.effort)
    }
    clearProviderSwitch(chatId)
    pendingSwitchPreviousRef.current = null
  }, [chatId])

  const isCodex = draftProvider === 'codex'
  const switchBusy = compacting
  const codexSwitchWarning = (
    isCodex && hasAssistantTurns
    && effective?.model && draftModel
    && draftModel !== effective?.model
  )

  const hiddenIds = prefs?.hidden_ids || []
  const selectedProvider = pendingSwitch?.provider ?? draftProvider
  const selectedModel = pendingSwitch?.model ?? draftModel
  const autoResumeSwitchId = chatId
    ? `chat-settings-auto-resume-${chatId}`
    : undefined
  const appProviderLocked = chat?.created_by_app_id != null

  // Build the per-provider displayed-models list once per render. The backend
  // registry owns both live discovery and its offline fallback; keeping a
  // second frontend catalog would let the two drift.
  const displayedByProvider = useMemo(() => {
    const out = {}
    for (const pid of PROVIDER_ORDER) {
      const live = registry?.[pid]
      const source = Array.isArray(live) ? live : []
      const selectedHere = selectedProvider === pid
        ? selectedModel
        : (draftProvider === pid ? draftModel : null)
      out[pid] = resolveDisplayedModels(source, hiddenIds, selectedHere)
    }
    return out
  }, [registry, hiddenIds, selectedModel, selectedProvider, draftModel, draftProvider])

  const currentProviderConfigured = availability.configuredProviders.has(draftProvider)
  const currentProviderLabel = PROVIDER_INFO[draftProvider]?.label || draftProvider

  return (
    <div className="csp">
      <div className="csp__label">Model</div>
      {!dataReady && (
        <>
          {modelLoadError ? (
            <div className="csp__availability-warning" role="alert">
              <span>Could not load models.</span>
              <button
                type="button"
                onClick={() => {
                  registryQuery.refetch()
                  prefsQuery.refetch()
                }}
              >
                Retry
              </button>
            </div>
          ) : (
            <div className="csp__loading" role="status" aria-live="polite">
              {loadingModels && <span className="csp__loading-spinner" aria-hidden="true" />}
              <span>Loading models…</span>
            </div>
          )}
          {!modelLoadError && (
            // Skeleton placeholder while registry + prefs resolve. Two
            // rows mirror the typical visible count without committing
            // to a specific model list — covers the case where prefs
            // hide most of the rows once they land.
            <div className="csp__skeleton" aria-hidden="true">
              <div className="csp__skeleton-row" />
              <div className="csp__skeleton-row" />
            </div>
          )}
        </>
      )}
      {dataReady && availability.phase === PROVIDER_AVAILABILITY_PHASE.ERROR && (
        <div className="csp__availability-warning" role="alert">
          <span>Could not verify providers. Showing the current model only.</span>
          <button
            type="button"
            onClick={() => providerStatusQuery.refetch()}
          >
            Retry
          </button>
        </div>
      )}
      {dataReady
        && availability.phase === PROVIDER_AVAILABILITY_PHASE.READY
        && !currentProviderConfigured && (
          <div className="csp__availability-warning" role="status">
            <span>
              {availability.configuredProviders.size > 0
                ? `${currentProviderLabel} isn’t connected. Choose a connected provider or reconnect it in Settings.`
                : `${currentProviderLabel} isn’t connected. Connect a provider in Settings.`}
            </span>
          </div>
      )}
      {dataReady && !selectedModel && (
        <div className="csp__selection-required" role="status" aria-live="polite">
          Choose a model before sending your message.
        </div>
      )}
      {dataReady && PROVIDER_ORDER.map(pid => {
        const info = PROVIDER_INFO[pid]
        const providerConfigured = availability.configuredProviders.has(pid)
        const models = visibleProviderModels(
          pid,
          displayedByProvider[pid] || [],
          availability.configuredProviders,
          draftProvider,
          draftModel,
        )
        if (!models.length) return null
        const isCrossProvider = hasAssistantTurns && pid !== draftProvider
        const appCrossProvider = (
          appProviderLocked
          && pid !== (provider || 'claude')
        )
        return models.map(m => {
          const rowEfforts = modelEfforts(info.efforts, m)
          const isPendingRow = pendingSwitch?.model === m.id && pendingSwitch?.provider === pid
          const isSelected = selectedModel === m.id && selectedProvider === pid
          return (
            <div key={`${pid}-${m.id}`}>
              <button
                type="button"
                className={`csp-row${isSelected ? ' csp-row--selected' : ''}`}
                onClick={() => {
                  if (!appCrossProvider) handlePickModel(m.id, pid, rowEfforts)
                }}
                disabled={switchBusy || appCrossProvider || !providerConfigured}
                aria-pressed={isSelected}
                title={appCrossProvider
                  ? 'App chats keep their original provider. Create a new app chat to use this provider.'
                  : (isCrossProvider ? 'Prepare this chat and switch providers' : undefined)}
              >
                <span className="csp-row__icon"><info.Logo /></span>
                <span className="csp-row__main">
                  <span className="csp-row__title">
                    <span>{m.label}</span>
                  </span>
                  <span className="csp-row__sub">
                    {providerConfigured ? info.label : `${info.label} · Not connected`}
                  </span>
                </span>
                <span className="csp-row__dot" />
              </button>
              {isSelected && !isPendingRow && (
                // Indent aligns the stepper under the row title (icon 30 +
                // gap 12 + row pad 10 = 52).
                <div className="csp-effort-indent">
                  <EffortStepper
                    efforts={rowEfforts}
                    value={draftEffort}
                    onChange={handleEffortChange}
                    // Routine picker writes are optimistic and serialized, so
                    // keep the shared control visually stable and available
                    // while a save settles. A live provider switch or
                    // disconnected provider remains genuinely unavailable.
                    disabled={switchBusy || !providerConfigured}
                  />
                </div>
              )}
              {isPendingRow && (
                <div className="csp__confirm" role="group" aria-label="Confirm provider switch">
                  <p className="csp__confirm-copy">
                    {info.label} will prepare its own context from this chat&apos;s history and running summary.
                  </p>
                  <div className="csp__confirm-actions">
                    <button
                      type="button"
                      className="csp__confirm-btn csp__confirm-btn--primary"
                      onClick={handleConfirmProviderSwitch}
                      disabled={switchBusy}
                    >
                      {switchBusy ? 'Preparing…' : 'Switch provider'}
                    </button>
                    <button
                      type="button"
                      className="csp__confirm-btn csp__confirm-btn--ghost"
                      onClick={handleCancelProviderSwitch}
                      disabled={switchBusy}
                    >
                      Cancel
                    </button>
                  </div>
                  {error && (
                    <p className="csp__error" role="alert">{error}</p>
                  )}
                </div>
              )}
            </div>
          )
        })
      })}
      {onAutoResumeChange && (
        <div className="csp__automation">
          <div className="csp__label csp__label--automation">Automation</div>
          <div className="csp__automation-row">
            <label className="csp__automation-copy" htmlFor={autoResumeSwitchId}>
              <span className="csp__automation-title">Automatically continue after usage limits</span>
            </label>
            <Switch
              className="chat-policy-switch"
              id={autoResumeSwitchId}
              checked={!!autoResumeEnabled}
              onCheckedChange={onAutoResumeChange}
              disabled={!!autoResumeSaving}
            />
          </div>
          {autoResumeError && (
            <p className="csp__automation-error" role="alert">
              {autoResumeError}
            </p>
          )}
        </div>
      )}
      {(
        appProviderLocked
        || codexSwitchWarning
        || switchBusy
        || (error && !pendingSwitch)
      ) && (
        <div className="csp__foot" aria-live="polite">
          {appProviderLocked && (
            <p className="csp__note">
              App chats keep their original provider. Create a new app chat
              to use another provider.
            </p>
          )}
          {switchBusy && (
            <p className="csp__note">
              Preparing this chat for {PROVIDER_INFO[pendingSwitch?.provider]?.label || 'the new provider'}…
            </p>
          )}
          {codexSwitchWarning && (
            <p className="csp__note">
              Codex injects a one-time model-switch note on the next
              reply — may briefly affect that turn.
            </p>
          )}
          {error && !pendingSwitch && (
            <p className="csp__error" role="alert">{error}</p>
          )}
        </div>
      )}
    </div>
  )
}
