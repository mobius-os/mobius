import { useState, useEffect, useCallback, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { ArrowLeft, Moon, Sun } from '@openai/apps-sdk-ui/components/Icon'
import GripVertical from 'lucide-react/dist/esm/icons/grip-vertical.mjs'
import { api, clearQueryCache, clearToken, getToken } from '../../api/client.js'
import { authQueries, modelQueries, settingsQueries, themeQueries } from '../../hooks/queries.js'
import { settleBackgroundAgentSave } from '../../lib/backgroundAgentSave.js'
import { connectedProvidersFirst, hasActiveConnectedProvider, moveConnectedProvider } from '../../lib/backgroundProviderOrder.js'
import { clearExplicitOwnerSession } from '../../lib/explicitLogout.js'
import { stopShellInstallPassPreparation } from '../../lib/shellInstallPass.js'
import { captureLayoutSpace, clientLengthToLayout } from '../../lib/layoutSpace.js'
import {
  PROVIDER_AVAILABILITY_PHASE,
  resolveProviderAvailability,
} from '../../lib/providerAvailability.js'
import * as themeService from '../../lib/themeService.js'
import ProviderAuth from '../ProviderAuth/ProviderAuth.jsx'
import CodexAuth from '../ProviderAuth/CodexAuth.jsx'
import ProviderRow from '../ProviderAuth/ProviderRow.jsx'
import ProviderConnection from '../ProviderAuth/ProviderConnection.jsx'
import ModelSheet from '../ui/ModelSheet.jsx'
import { modelEfforts, validEffort } from '../ui/modelEfforts.js'
import ManageModelsModal from '../ChatView/ManageModelsModal.jsx'
import PlatformUpdates from './PlatformUpdates.jsx'
import ProviderUsage from './ProviderUsage.jsx'
import GithubConnection from './GithubConnection.jsx'
import IdentityAccount, { ProfileAvatar } from './identity/IdentityAccount.jsx'
import { useIdentityQuery } from './identity/identity-client.js'
import MobiusProviderAccess from './identity/MobiusProviderAccess.jsx'
import { formatPlanStatus } from './providerUsage.js'
import { PROVIDER_INFO, PROVIDER_ORDER, providerInfoFor, providerOrderFor } from '../ChatView/providerRegistry.jsx'
import { saveThemeThenRefreshStatusBar } from '../../lib/statusBarThemeReload.js'
import '../ui/StatusDot.css'
import '../ui/ModelSheet.css'
import './SettingsView.css'

const PROVIDER_CHOICES = [
  { id: 'mobius', label: 'Möbius' },
  { id: 'claude', label: 'Claude Code' },
  { id: 'codex', label: 'OpenAI Codex' },
]
const DEFAULT_BACKGROUND_MODELS = {
  mobius: 'evolve',
  claude: 'claude-opus-4-8',
  codex: 'gpt-5.6-terra',
}

function defaultEffort(provider) {
  const efforts = PROVIDER_INFO[provider]?.efforts || []
  return efforts.find(e => e.value === 'medium')?.value || efforts[0]?.value || ''
}

function defaultBackgroundModel(provider) {
  return DEFAULT_BACKGROUND_MODELS[provider] || ''
}

function isKnownProvider(provider) {
  return PROVIDER_CHOICES.some(p => p.id === provider) || /^app-\d+$/.test(provider || '')
}

function providerFromSettings(settings) {
  return isKnownProvider(settings?.provider) ? settings.provider : 'claude'
}


function normalizeBackgroundAgents(backgroundAgents, defaultProvider = 'claude') {
  const rows = []
  const seen = new Set()
  const resolvedDefaultProvider = isKnownProvider(defaultProvider)
    ? defaultProvider
    : 'claude'

  const addChoice = (choice, enabledDefault) => {
    const provider = isKnownProvider(choice?.provider) ? choice.provider : null
    if (!provider || seen.has(provider)) return
    rows.push({
      provider,
      model: choice?.model || defaultBackgroundModel(provider),
      effort: choice?.effort || defaultEffort(provider),
      enabled: Object.prototype.hasOwnProperty.call(choice || {}, 'enabled')
        ? choice.enabled !== false
        : enabledDefault,
    })
    seen.add(provider)
  }

  if (Array.isArray(backgroundAgents?.providers)) {
    backgroundAgents.providers.forEach((choice) => addChoice(choice, true))
  }

  if (!rows.length) addChoice({ provider: resolvedDefaultProvider }, true)
  PROVIDER_ORDER.forEach((provider) => addChoice({ provider }, false))
  if (!rows.some(row => row.enabled)) rows[0].enabled = true
  return rows
}

function BackgroundProviderRow({
  row,
  providerInfo,
  index,
  models,
  dragging,
  dropTarget,
  dragStyle,
  reorderMode,
  rowRef,
  onModelChange,
  onEffortChange,
  onMove,
  onReorderStart,
  configuredProviders,
  detailMode,
  onOpen,
  details,
}) {
  const info = providerInfo || providerInfoFor(row.provider)
  const Logo = info?.Logo
  const configured = configuredProviders.has(row.provider)
  const enabled = configured && row.enabled !== false
  const selectedModel = enabled
    ? (row.model || defaultBackgroundModel(row.provider))
    : ''
  const selectedRow = models.find((m) => m.id === selectedModel)
  const efforts = info?.efforts || []
  const selectedEfforts = modelEfforts(efforts, selectedRow)
  const effortLabel = enabled
    ? (selectedEfforts.find((e) => e.value === row.effort)?.label || '')
    : ''
  const selectedEffortIndex = Math.max(
    0,
    selectedEfforts.findIndex((effort) => effort.value === row.effort),
  )
  const [sheetOpen, setSheetOpen] = useState(false)

  // This row is a fixed provider, so the sheet shows only that
  // provider's models plus a "None" row that disables the background
  // agent. Effort lives inside the sheet, under the selected model:
  // providers expose different effort scales, so it belongs with the
  // model choice (mirrors the chat composer's picker).
  const groups = [{
    key: row.provider,
    label: info?.label || row.provider,
    Logo,
    models: configured ? models : [],
  }]
  const triggerLabel = enabled
    ? (selectedRow?.label || selectedModel || 'Choose model')
    : 'None'
  return (
    <div
      ref={rowRef}
      className={
        'settings-bg-row'
        + (detailMode ? ' settings-bg-row--detail' : '')
        + (enabled ? '' : ' settings-bg-row--off')
        + (configured ? '' : ' settings-bg-row--disconnected')
        + (reorderMode ? ' settings-bg-row--reordering' : '')
        + (dragging ? ' settings-bg-row--dragging' : '')
        + (dropTarget ? ' settings-bg-row--drop-target' : '')
      }
      style={dragStyle}
      aria-label={detailMode ? `${info?.label || row.provider} settings` : configured ? `${info?.label || row.provider} background priority ${index + 1}` : `${info?.label || row.provider}, not connected`}
    >
      {reorderMode && configured && (
        <button
          type="button"
          className="settings-bg-row__drag-handle"
          aria-label={`Move ${info?.label || row.provider} background priority`}
          onPointerDown={(event) => {
            if (event.button !== undefined && event.button !== 0) return
            event.preventDefault()
            event.stopPropagation()
            onReorderStart(index, {
              clientY: event.clientY,
              pointerId: event.pointerId,
              captureNode: event.currentTarget,
            })
          }}
          onClick={(event) => event.preventDefault()}
          onKeyDown={(event) => {
            if (event.key === 'ArrowUp') {
              event.preventDefault()
              onMove(-1)
            } else if (event.key === 'ArrowDown') {
              event.preventDefault()
              onMove(1)
            }
          }}
        >
          {/* The SDK has no drag/reorder glyph; DotsVertical is a menu action. */}
          <GripVertical size={18} strokeWidth={2} aria-hidden="true" />
        </button>
      )}
      {reorderMode && !configured && <span className="settings-bg-row__drag-spacer" aria-hidden="true" />}
      <div className="settings-bg-row__body">
        {!detailMode && <button
          type="button"
          className="settings-provider-summary"
          onClick={onOpen}
          aria-label={`${info?.label || row.provider} provider settings`}
        >
          <span className="settings-provider-summary__icon">{Logo ? <Logo /> : (row.provider[0] || '?').toUpperCase()}</span>
          <span className="settings-provider-summary__copy">
            <span>{info?.label || row.provider}</span>
            <small>{configured ? `Background: ${triggerLabel}` : 'Not connected'}</small>
          </span>
          {configured && <span className="settings-provider-summary__rank">{index + 1}</span>}
          <span className="settings-provider-summary__arrow" aria-hidden="true">›</span>
        </button>}
        {detailMode && <div className="settings-provider-details">
          {details}
          <div className="settings-provider-details__model">
            <span>Background agent</span>
            <button
              type="button"
              className={`model-trigger${enabled ? '' : ' model-trigger--off'}`}
              title={selectedModel || triggerLabel}
              onClick={() => setSheetOpen(true)}
              disabled={!configured}
              aria-haspopup="dialog"
              aria-label={`${info?.label || row.provider} background model${effortLabel ? `, ${effortLabel} effort` : ''}`}
            >
              <span className="model-trigger__icon">
                {Logo ? <Logo /> : (row.provider[0] || '?').toUpperCase()}
              </span>
              <span className="model-trigger__main">
                <span className="model-trigger__name">{triggerLabel}</span>
                {enabled && selectedModel && (
                  <span className="model-trigger__id">{selectedModel}</span>
                )}
              </span>
              {enabled && effortLabel && (
                <span className="settings-bg-row__effort-visual" aria-hidden="true">
                  {selectedEfforts.map((effort, effortIndex) => (
                    <span
                      key={effort.value}
                      className={
                        'settings-bg-row__effort-dot'
                        + (effortIndex <= selectedEffortIndex ? ' settings-bg-row__effort-dot--filled' : '')
                        + (effortIndex === selectedEffortIndex ? ' settings-bg-row__effort-dot--on' : '')
                      }
                    />
                  ))}
                </span>
              )}
            </button>
          </div>
        </div>}
      </div>
      <ModelSheet
        open={sheetOpen}
        onClose={() => setSheetOpen(false)}
        title={`${info?.label || row.provider} model`}
        groups={groups}
        provider={enabled ? row.provider : ''}
        model={selectedModel}
        efforts={efforts}
        effort={row.effort}
        configuredProviders={configuredProviders}
        onEffortChange={onEffortChange}
        onPick={(pid, id, pickedModel) => {
          const nextEfforts = modelEfforts(efforts, pickedModel)
          onModelChange(id, validEffort(nextEfforts, row.effort))
        }}
        allowNone
        noneLabel="None (disable)"
        onNone={() => onModelChange('')}
      />
    </div>
  )
}

export default function SettingsView({
  onThemeChange,
  onOpenChat,
  onOpenApp,
  focusTarget = null,
  active = true,
  refreshToken = 0,
  onLeaveSharedAccess = null,
  onStatusBarThemeReload = null,
}) {
  const settingsBoundaryRef = useRef(null)
  const queryClient = useQueryClient()
  const settingsQuery = settingsQueries.owner.useQuery()
  const providerStatusQuery = authQueries.provider.statuses.useQuery()
  const themeModeQuery = themeQueries.mode.useQuery()
  const [themeMode, setThemeMode] = useState(() => (
    typeof document !== 'undefined'
    && document.documentElement.getAttribute('data-theme') === 'light'
      ? 'light'
      : 'dark'
  ))
  const [themeSwitching, setThemeSwitching] = useState(false)
  // Which provider has its inline auth panel expanded. null = none.
  const [expandedAuth, setExpandedAuth] = useState(null)
  // Usage is on demand: opening a provider detail fetches only that provider.
  // Surface failures from the dark-mode toggle: a failed theme
  // persist would otherwise bounce the knob without telling the user
  // why.
  const [themeError, setThemeError] = useState('')
  const [signOutConfirm, setSignOutConfirm] = useState(false)
  const [signingOut, setSigningOut] = useState(false)
  const [mobiusAccountOpen, setMobiusAccountOpen] = useState(false)
  const [githubAccountOpen, setGithubAccountOpen] = useState(false)
  const [selectedProvider, setSelectedProvider] = useState(null)

  // Shared with the account page, so the row keeps the last confirmed profile
  // when the identity service is unavailable and reflects edits immediately.
  const mobiusProfile = useIdentityQuery(getToken(), { enabled: active }).data?.profile ?? null

  useEffect(() => {
    // Mirror the full query value so a cache invalidation that
    // resolves to 'dark' actually updates the selected option. The earlier
    // light-only branch left the control stuck whenever data went
    // light → dark via refetch (e.g. another tab changed it, or a
    // failed persist's rollback landed via invalidation).
    if (themeModeQuery.data === undefined) return
    setThemeMode(themeModeQuery.data === 'light' ? 'light' : 'dark')
  }, [themeModeQuery.data])

  const providerAvailability = resolveProviderAvailability(providerStatusQuery)
  const configuredProviders = providerAvailability.configuredProviders
  const codexAuthenticated = configuredProviders.has('codex')
  // Live-probed CLI versions (null when the CLI isn't installed or
  // didn't respond). Read-only — updates happen via the agent, not here.
  const claudeVersion = settingsQuery.data?.claude_version
  const codexVersion = settingsQuery.data?.codex_version
  const claudeAuthenticated = configuredProviders.has('claude')
  // Three-state gate for the AI-providers section, in priority order:
  //
  //   READY   — at least the cached data is present (data !== undefined).
  //             Both queries are persisted to IndexedDB and hydrate
  //             before the network round-trip (see queryClient.js), so on
  //             a re-open we paint from disk instantly and let the
  //             background revalidation update the rows only if something
  //             changed. This is why we gate on `data`, not `isFetched`:
  //             `isFetched` is still false on this mount even when the
  //             cache already holds a value, and gating on it reintroduced
  //             the open-time flash this fix removes.
  //   ERROR   — no data at all AND the fetch failed. A first-ever open
  //             with no persisted cache that errors must say so, not
  //             render the section blank with no indication.
  //   LOADING — no data yet and no error: the initial in-flight fetch.
  const providerReady = settingsQuery.data !== undefined
    && providerAvailability.phase === PROVIDER_AVAILABILITY_PHASE.READY
  const codexUsageQuery = settingsQueries.providerUsage.useQuery('codex', {
    enabled: (
      active && providerReady && codexAuthenticated
      && selectedProvider === 'codex'
    ),
  })
  const [codexRedeem, setCodexRedeem] = useState({ busy: false, result: null })
  const handleRedeemCodexReset = useCallback(async (creditId = null) => {
    setCodexRedeem({ busy: true, result: null })
    try {
      const res = await api.settings.redeemCodexReset(creditId)
      if (!res.ok) throw new Error('redeem failed')
      const data = await res.json()
      setCodexRedeem({ busy: false, result: { outcome: data?.outcome } })
      // Refetch so the window fills and the banked count both reflect the redeem.
      settingsQueries.providerUsage.invalidate(queryClient, 'codex')
    } catch {
      setCodexRedeem({ busy: false, result: { error: true } })
    }
  }, [queryClient])
  const claudeUsageQuery = settingsQueries.providerUsage.useQuery('claude', {
    enabled: (
      active && providerReady && claudeAuthenticated
      && selectedProvider === 'claude'
    ),
  })
  const [claudeExtraUsage, setClaudeExtraUsage] = useState({ busy: false, result: null })
  const [claudeResetRedeem, setClaudeResetRedeem] = useState({ busy: false, result: null })
  const handleRedeemClaudeReset = useCallback(async (creditId, expectedResetsLeft) => {
    setClaudeResetRedeem({ busy: true, result: null })
    try {
      const res = await api.settings.redeemClaudeReset(creditId, expectedResetsLeft)
      if (res.status === 409) {
        setClaudeResetRedeem({ busy: false, result: { outcome: 'offer_changed' } })
        settingsQueries.providerUsage.invalidate(queryClient, 'claude')
        return
      }
      if (!res.ok) throw new Error('Claude reset redeem failed')
      const data = await res.json()
      setClaudeResetRedeem({ busy: false, result: { outcome: data?.outcome } })
      settingsQueries.providerUsage.invalidate(queryClient, 'claude')
    } catch {
      setClaudeResetRedeem({ busy: false, result: { error: true } })
    }
  }, [queryClient])
  const handleClaudeExtraUsage = useCallback(async (enabled, expectedEnabled) => {
    setClaudeExtraUsage({ busy: true, result: null })
    try {
      const res = await api.settings.setClaudeExtraUsage(enabled, expectedEnabled)
      if (!res.ok) throw new Error('Claude extra usage update failed')
      const snapshot = await res.json()
      queryClient.setQueryData(
        settingsQueries.providerUsage.keyFor('claude'),
        snapshot,
      )
      setClaudeExtraUsage({ busy: false, result: { enabled } })
      settingsQueries.providerUsage.invalidate(queryClient, 'claude')
    } catch {
      setClaudeExtraUsage({ busy: false, result: { error: true } })
    }
  }, [queryClient])
  // Registry and provider/settings probes are independent. Starting them
  // together avoids an unnecessary request waterfall on a first open.
  const modelRegistryQuery = modelQueries.registry.useQuery()
  const modelProviderOrder = providerOrderFor(modelRegistryQuery.data)
  const modelProviderInfo = Object.fromEntries(modelProviderOrder.map(id => [
    id, providerInfoFor(id, providerStatusQuery.data),
  ]))
  const providerError =
    !providerReady && (settingsQuery.isError || providerStatusQuery.isError)
  const providerErrorMsg =
    settingsQuery.error?.message || providerStatusQuery.error?.message ||
    'Could not load provider settings.'
  const retryProviders = useCallback(() => {
    settingsQuery.refetch()
    providerStatusQuery.refetch()
  }, [settingsQuery, providerStatusQuery])
  const [backgroundDraft, setBackgroundDraft] = useState(null)
  const backgroundDraftRef = useRef(null)
  const [backgroundError, setBackgroundError] = useState('')
  const backgroundSaveReqRef = useRef(0)
  const backgroundSaveChainRef = useRef(Promise.resolve())
  const [backgroundDrag, setBackgroundDrag] = useState(null)
  const [backgroundCommitting, setBackgroundCommitting] = useState(false)
  const backgroundDragRef = useRef(null)
  const backgroundCommitRafRef = useRef(null)
  const backgroundRowRefs = useRef([])
  // Latest finger Y during a drag. Updated imperatively on every
  // pointermove so the held row can follow the pointer 1:1 without a
  // React re-render per frame; render reads it for the same transform.
  const backgroundPointerYRef = useRef(0)
  const [manageModelsProvider, setManageModelsProvider] = useState(null)
  const setupFocusRefs = useRef({})
  const [attentionSection, setAttentionSection] = useState('')
  const configuredProvidersRef = useRef(configuredProviders)
  const authProvidersAtStartRef = useRef(null)
  configuredProvidersRef.current = configuredProviders

  const setSetupFocusRef = useCallback((section, node) => {
    if (node) setupFocusRefs.current[section] = node
  }, [])

  useEffect(() => {
    if (!settingsQuery.data) return
    const next = normalizeBackgroundAgents(
      settingsQuery.data.background_agents,
      providerFromSettings(settingsQuery.data),
    )
    backgroundDraftRef.current = next
    setBackgroundDraft(next)
  }, [settingsQuery.data])

  useEffect(() => {
    const requested = focusTarget?.section
    if (!requested) return undefined
    const section = requested === 'models' ? 'ai-providers' : requested
    if (requested === 'models') setManageModelsProvider('all')
    let clearTimer = null
    const raf = requestAnimationFrame(() => {
      const node = setupFocusRefs.current[section]
      if (!node) return
      node.scrollIntoView({ behavior: 'smooth', block: 'start' })
      node.focus({ preventScroll: true })
      setAttentionSection(section)
      clearTimer = setTimeout(() => {
        setAttentionSection((current) => current === section ? '' : current)
      }, 1800)
    })
    return () => {
      cancelAnimationFrame(raf)
      if (clearTimer) clearTimeout(clearTimer)
    }
  }, [focusTarget, providerReady])

  const modelsForProvider = useCallback((provider) => {
    if (!provider) return []
    const rows = Array.isArray(modelRegistryQuery.data?.[provider])
      ? modelRegistryQuery.data[provider]
      : null
    if (rows && rows.length) {
      return rows.map((m) => ({
        id: m.id,
        label: m.label || m.id,
        available: m.available,
        effort_levels: m.effort_levels,
      }))
    }
    return []
  }, [modelRegistryQuery.data])

  const persistBackgroundAgents = useCallback((draft, companionSettings = {}) => {
    const rows = Array.isArray(draft) ? draft : []
    const enabled = rows.filter(row => row.enabled !== false)
    if (!enabled.length) {
      setBackgroundError('Some services are now disabled.')
      return Promise.resolve(false)
    }
    const reqId = ++backgroundSaveReqRef.current
    const isCompanionSave = Object.keys(companionSettings).length > 0
    setBackgroundError('')
    const save = backgroundSaveChainRef.current.catch(() => {}).then(async () => {
      try {
        const toChoice = (row, includeEnabled = false) => {
          const isEnabled = row.enabled !== false
          const choice = {
            provider: row.provider,
            model: isEnabled ? (row.model || null) : null,
            effort: isEnabled ? (row.effort || null) : null,
          }
          if (includeEnabled) choice.enabled = isEnabled
          return choice
        }
        const payload = {
          providers: rows.map(row => toChoice(row, true)),
        }
        // A first provider connection also establishes the interactive default.
        // Keep that transition in one settings write so disk failure cannot
        // persist one half while the UI reports the whole setup as complete.
        const res = await api.settings.save({
          ...companionSettings,
          background_agents: payload,
        })
        const { stale } = await settleBackgroundAgentSave(
          res,
          () => reqId !== backgroundSaveReqRef.current,
        )
        if (stale) return true
        settingsQueries.owner.invalidate(queryClient)
        return true
      } catch (err) {
        if (reqId === backgroundSaveReqRef.current || isCompanionSave) {
          setBackgroundError(err.message || 'Could not save background agents.')
        }
        return false
      }
    })
    backgroundSaveChainRef.current = save
    return save
  }, [queryClient])

  const updateBackgroundDraft = useCallback((updater) => {
    const current = connectedProvidersFirst(backgroundDraftRef.current ||
      normalizeBackgroundAgents(
        settingsQuery.data?.background_agents,
        providerFromSettings(settingsQuery.data),
      ), configuredProvidersRef.current)
    const next = typeof updater === 'function' ? updater(current) : updater
    const ordered = connectedProvidersFirst(next, configuredProvidersRef.current)
    if (!hasActiveConnectedProvider(ordered, configuredProvidersRef.current)) {
      setBackgroundError('Keep at least one connected background provider active.')
      return
    }
    backgroundDraftRef.current = ordered
    setBackgroundDraft(ordered)
    persistBackgroundAgents(ordered)
  }, [persistBackgroundAgents, settingsQuery.data])

  const setBackgroundProviderChoice = useCallback((provider, patch) => {
    updateBackgroundDraft((current) => current.map((row) => {
      if (row.provider !== provider) return row
      return { ...row, ...patch }
    }))
  }, [updateBackgroundDraft])

  const moveBackgroundProvider = useCallback((fromIndex, toIndex) => {
    const current = backgroundDraftRef.current ||
      normalizeBackgroundAgents(
        settingsQuery.data?.background_agents,
        providerFromSettings(settingsQuery.data),
      )
    const next = moveConnectedProvider(current, fromIndex, toIndex, configuredProvidersRef.current)
    if (next) updateBackgroundDraft(next)
  }, [settingsQuery.data, updateBackgroundDraft])

  useEffect(() => {
    backgroundDragRef.current = backgroundDrag
  }, [backgroundDrag])

  const beginBackgroundCommit = useCallback(() => {
    if (backgroundCommitRafRef.current) {
      window.cancelAnimationFrame(backgroundCommitRafRef.current)
      backgroundCommitRafRef.current = null
    }
    setBackgroundCommitting(true)
    backgroundCommitRafRef.current = window.requestAnimationFrame(() => {
      backgroundCommitRafRef.current = window.requestAnimationFrame(() => {
        backgroundCommitRafRef.current = null
        setBackgroundCommitting(false)
      })
    })
  }, [])

  const backgroundIndexFromY = useCallback((pointerY, rows) => {
    if (!rows.length) return 0
    // Partition by the MIDPOINT between adjacent slot centers, not the
    // centers themselves: the dragged row's projected center starts on its
    // own slot center, so a center-based test would flip to the next slot
    // on the very first pixel of travel (and snap a full row on release).
    // Midpoints mean "drag past halfway to swap" — a tiny nudge eases back.
    for (let index = 0; index < rows.length - 1; index++) {
      const boundary = (rows[index].center + rows[index + 1].center) / 2
      if (pointerY < boundary) return index
    }
    return rows.length - 1
  }, [])

  // Called by a row once its press-and-hold elapses. `pointer` carries
  // the captured hold position/id (the original pointerdown event is
  // long gone by the time the hold timer fires).
  const startBackgroundReorder = useCallback((index, pointer) => {
    const current = backgroundDraftRef.current || []
    const connectedCount = current.filter(row => configuredProvidersRef.current.has(row.provider)).length
    if (index >= connectedCount) return
    const node = backgroundRowRefs.current[index] || pointer?.node
    if (!node) return
    const layoutSpace = captureLayoutSpace(node)
    const toLayoutY = clientY => clientLengthToLayout(
      clientY - layoutSpace.clientTop,
      layoutSpace,
    )
    const rowRect = node.getBoundingClientRect()
    const rows = backgroundRowRefs.current.slice(0, connectedCount)
    const slots = rows.map((rowNode) => {
      if (!rowNode) return null
      const rect = rowNode.getBoundingClientRect()
      const top = toLayoutY(rect.top)
      const height = clientLengthToLayout(rect.height, layoutSpace)
      return {
        top,
        height,
        center: top + height / 2,
      }
    }).filter(Boolean)
    const captureNode = pointer?.captureNode || node
    try { captureNode.setPointerCapture?.(pointer?.pointerId) } catch { /* best-effort */ }
    const grabY = toLayoutY(
      typeof pointer?.clientY === 'number' ? pointer.clientY : rowRect.top,
    )
    backgroundPointerYRef.current = grabY
    const next = {
      fromIndex: index,
      toIndex: index,
      grabOffsetY: grabY - toLayoutY(rowRect.top),
      rowHeight: clientLengthToLayout(rowRect.height, layoutSpace),
      slots,
      layoutSpace,
    }
    setBackgroundDrag(next)
  }, [])

  const activeBackgroundDragFromIndex = backgroundDrag?.fromIndex ?? null
  useEffect(() => {
    if (activeBackgroundDragFromIndex === null) return undefined
    // Constant for the life of one drag — captured once so pointer
    // handlers never read stale React state.
    const start = backgroundDragRef.current
    if (!start) return undefined
    const { fromIndex, grabOffsetY, rowHeight, slots, layoutSpace } = start
    const originTop = slots[fromIndex]?.top ?? 0
    const minOffset = slots.length ? slots[0].top - originTop : 0
    const maxOffset = slots.length ? slots[slots.length - 1].top - originTop : 0
    const followOffset = (pointerY) => {
      const raw = pointerY - grabOffsetY - originTop
      return Math.max(minOffset, Math.min(maxOffset, raw))
    }
    const toLayoutY = clientY => clientLengthToLayout(
      clientY - layoutSpace.clientTop,
      layoutSpace,
    )

    const onPointerMove = (event) => {
      event.preventDefault()
      const current = backgroundDragRef.current
      if (!current) return
      const pointerY = toLayoutY(event.clientY)
      backgroundPointerYRef.current = pointerY
      // Move the held row imperatively so it tracks the finger 1:1
      // without re-rendering the whole panel every frame.
      const node = backgroundRowRefs.current[fromIndex]
      if (node) {
        node.style.transform = `translateY(${followOffset(pointerY)}px) scale(1.02)`
      }
      // Only re-render (to slide the other rows aside) when the target
      // slot actually changes.
      const dragCenterY = pointerY - grabOffsetY + rowHeight / 2
      const toIndex = backgroundIndexFromY(dragCenterY, slots)
      setBackgroundDrag((c) => (
        c && c.toIndex !== toIndex ? { ...c, toIndex } : c
      ))
    }

    const finish = (event) => {
      event.preventDefault()
      const current = backgroundDragRef.current
      if (!current) return
      const pointerY = toLayoutY(event.clientY)
      backgroundPointerYRef.current = pointerY
      const dragCenterY = pointerY - grabOffsetY + rowHeight / 2
      const toIndex = backgroundIndexFromY(dragCenterY, slots)
      // Commit synchronously on release. If the row didn't change slots,
      // just drop the drag and let the base CSS transition ease it back
      // into place; otherwise reorder under the no-transition "committing"
      // guard so the displaced rows don't flip-flash.
      if (toIndex === fromIndex) {
        setBackgroundDrag(null)
        return
      }
      beginBackgroundCommit()
      setBackgroundDrag(null)
      moveBackgroundProvider(fromIndex, toIndex)
    }

    const cancel = () => {
      setBackgroundDrag(null)
    }

    window.addEventListener('pointermove', onPointerMove, { passive: false })
    window.addEventListener('pointerup', finish, { passive: false })
    window.addEventListener('pointercancel', cancel)
    return () => {
      window.removeEventListener('pointermove', onPointerMove)
      window.removeEventListener('pointerup', finish)
      window.removeEventListener('pointercancel', cancel)
    }
  }, [activeBackgroundDragFromIndex, backgroundIndexFromY, beginBackgroundCommit, moveBackgroundProvider])

  useEffect(() => () => {
    if (backgroundCommitRafRef.current) {
      window.cancelAnimationFrame(backgroundCommitRafRef.current)
      backgroundCommitRafRef.current = null
    }
  }, [])

  // Keep connection panels independent of the read-only usage detail.
  const toggleClaudeAuth = useCallback(
    () => {
      setExpandedAuth(prev => {
        if (prev !== 'claude') {
          authProvidersAtStartRef.current = new Set(configuredProvidersRef.current)
        }
        return prev === 'claude' ? null : 'claude'
      })
    },
    [],
  )
  const toggleCodexAuth = useCallback(
    () => {
      setExpandedAuth(prev => {
        if (prev !== 'codex') {
          authProvidersAtStartRef.current = new Set(configuredProvidersRef.current)
        }
        return prev === 'codex' ? null : 'codex'
      })
    },
    [],
  )
  const openMobiusYou = useCallback(() => {
    setMobiusAccountOpen(true)
    settingsBoundaryRef.current?.scrollTo({ top: 0 })
  }, [])
  const toggleGithub = useCallback(() => {
    setGithubAccountOpen(value => !value)
  }, [])
  const expandGithub = useCallback(() => setGithubAccountOpen(true), [])
  const onProviderConnected = useCallback(async (provider) => {
    const providersBefore = authProvidersAtStartRef.current || configuredProviders
    const newlyConnected = !providersBefore.has(provider)
    if (newlyConnected) {
      const current = backgroundDraftRef.current || normalizeBackgroundAgents(
        settingsQuery.data?.background_agents,
        providerFromSettings(settingsQuery.data),
      )
      const connectedRow = {
        ...(current.find(row => row.provider === provider) || { provider }),
        enabled: true,
        model: defaultBackgroundModel(provider),
        effort: defaultEffort(provider),
      }
      const rest = current.filter(row => row.provider !== provider)
      const next = providersBefore.size === 0
        ? [connectedRow, ...rest.map(row => ({ ...row, enabled: false }))]
        : current.map(row => row.provider === provider ? connectedRow : row)
      const ordered = connectedProvidersFirst(next, new Set([...configuredProviders, provider]))
      backgroundDraftRef.current = ordered
      setBackgroundDraft(ordered)
      const saved = await persistBackgroundAgents(
        ordered,
        providersBefore.size === 0 ? { provider } : {},
      )
      // Authentication itself succeeded, but keep the panel and visible error
      // in place until its associated defaults are durably saved.
      if (!saved) return
    }
    authProvidersAtStartRef.current = null
    settingsQueries.owner.invalidate(queryClient)
    settingsQueries.providerUsage.invalidate(queryClient, provider)
    setExpandedAuth(null)
  }, [configuredProviders, persistBackgroundAgents, queryClient, settingsQuery.data])
  const onProviderDisconnected = useCallback(() => {
    authProvidersAtStartRef.current = null
    setExpandedAuth(null)
  }, [])
  const onClaudeAuthDone = useCallback(() => {
    onProviderConnected('claude')
  }, [onProviderConnected])
  const onCodexAuthDone = useCallback(() => {
    onProviderConnected('codex')
  }, [onProviderConnected])

  async function toggleTheme() {
    if (themeSwitching) return

    // Derive the direction from what the user ACTUALLY SEES, not from
    // the optimistic `themeMode` state. `themeMode` mirrors
    // themeModeQuery.data, which resolves async through the SW and
    // LAGS the painted theme; trusting it computed the toggle in the
    // wrong direction (e.g. after a dark→light toggle, a follow-up
    // light→dark would re-derive 'light' from the stale state and
    // hand applyThemeToDom the already-current CSS → no-op repaint,
    // leaving the UI stuck). getEffectiveTheme().mode reads
    // <html data-theme> — the authoritative value applyThemeToDom
    // last painted — so the direction is always relative to the
    // visible theme. Fall back to `themeMode` only at very early boot
    // before any theme has been applied (mode === null).
    const eff = themeService.getEffectiveTheme()
    const currentMode = eff?.mode === 'light' || eff?.mode === 'dark'
      ? eff.mode
      : themeMode

    // Do not flip the icon state ahead of the palette. toggleTheme seeds the
    // theme-mode query immediately before it applies the new CSS; the mirror
    // effect above then updates the icon in the same repaint sequence. The old
    // local optimistic flip made the control's outlines move first, followed
    // by the rest of Settings after query cancellation completed.
    setThemeSwitching(true)
    setThemeError('')

    // Delegate the full apply/persist/invalidate dance to
    // themeService — SettingsView keeps only error and busy state while the
    // theme query remains the visual source of truth for the icon.
    // catch-rollback. themeService.toggleTheme invalidates both
    // theme queries; AppCanvas's useEffect picks that up and
    // postMessages `moebius:frame-theme` to live iframes.
    // An installed iPhone app re-reads its status-bar colour only on load, so
    // after the save the helper runs the shell's controlled reload there. A
    // reload failure never reaches this catch or the theme rollback.
    try {
      await saveThemeThenRefreshStatusBar({
        save: () => themeService.toggleTheme(queryClient, currentMode, api),
        reload: onStatusBarThemeReload,
      })
    } catch {
      setThemeMode(currentMode)
      setThemeError(
        'Could not save theme. Check your connection and try again.',
      )
      // Force the mode query to resync with the server. Covers the
      // write-succeeded-but-response-lost case: refetching reads
      // authoritative state, the themeModeQuery.data mirror effect above
      // picks it up, and themeMode stops disagreeing with the visible theme.
      themeQueries.mode.invalidate(queryClient)
      onThemeChange?.()  // reload original theme on error
    } finally {
      setThemeSwitching(false)
    }
  }

  async function signOut() {
    if (signingOut) return
    if (onLeaveSharedAccess) {
      await onLeaveSharedAccess()
      return
    }
    setSigningOut(true)
    try {
      await clearExplicitOwnerSession({
        stopInstallHandoffPreparation: stopShellInstallPassPreparation,
        revokeInstallHandoffs: () => api.auth.shellInstallPass.revoke(),
        dropCredential: clearToken,
        clearOwnerCache: clearQueryCache,
      })
    } finally {
      window.location.reload()
    }
  }

  const effectiveBackgroundDraft = connectedProvidersFirst(backgroundDraft ||
    normalizeBackgroundAgents(
      settingsQuery.data?.background_agents,
      providerFromSettings(settingsQuery.data),
    ), configuredProviders)
  useEffect(() => {
    backgroundRowRefs.current.length = effectiveBackgroundDraft.length
  }, [effectiveBackgroundDraft.length])
  const backgroundDragStyleForIndex = (index) => {
    if (!backgroundDrag) return undefined
    const slots = backgroundDrag.slots || []
    if (index === backgroundDrag.fromIndex) {
      const originSlot = slots[backgroundDrag.fromIndex]
      // The held row follows the finger 1:1. `transition:none` keeps it
      // pinned under the pointer with no easing lag; on release the style
      // is dropped and the base CSS transition eases it into its slot.
      const originTop = originSlot ? originSlot.top : 0
      const raw = (backgroundPointerYRef.current - backgroundDrag.grabOffsetY) - originTop
      const minOffset = slots.length ? slots[0].top - originTop : 0
      const maxOffset = slots.length ? slots[slots.length - 1].top - originTop : 0
      const offset = Math.max(minOffset, Math.min(maxOffset, raw))
      return {
        transform: `translateY(${offset}px) scale(1.02)`,
        zIndex: 3,
        transition: 'none',
      }
    }
    if (
      backgroundDrag.toIndex > backgroundDrag.fromIndex
      && index > backgroundDrag.fromIndex
      && index <= backgroundDrag.toIndex
    ) {
      const originSlot = slots[index]
      const targetSlot = slots[index - 1]
      const offset = originSlot && targetSlot ? targetSlot.top - originSlot.top : 0
      return { transform: `translateY(${offset}px)` }
    }
    if (
      backgroundDrag.toIndex < backgroundDrag.fromIndex
      && index >= backgroundDrag.toIndex
      && index < backgroundDrag.fromIndex
    ) {
      const originSlot = slots[index]
      const targetSlot = slots[index + 1]
      const offset = originSlot && targetSlot ? targetSlot.top - originSlot.top : 0
      return { transform: `translateY(${offset}px)` }
    }
    return undefined
  }
  const codexPlanLabel = (
    codexUsageQuery.data?.plan_label
    || settingsQuery.data?.provider_plans?.codex
    || ''
  )
  const claudePlanLabel = (
    claudeUsageQuery.data?.plan_label
    || settingsQuery.data?.provider_plans?.claude
    || ''
  )

  return (
    <div ref={settingsBoundaryRef} className="settings">
      <div className="settings__content">
        <header className="settings__header">
          {(selectedProvider || mobiusAccountOpen) && <button type="button" className="settings__back" aria-label="Back to Settings" onClick={() => { setSelectedProvider(null); setMobiusAccountOpen(false) }}><ArrowLeft width={18} height={18} aria-hidden="true" /></button>}
          <h1 className="settings__title">{mobiusAccountOpen ? 'Möbius account' : selectedProvider ? (modelProviderInfo[selectedProvider]?.label || selectedProvider) : 'Settings'}</h1>
          {!selectedProvider && !mobiusAccountOpen && <button
            type="button"
            className="settings__appearance-toggle"
            role="switch"
            aria-label="Dark mode"
            aria-checked={themeMode === 'dark'}
            aria-busy={themeSwitching}
            disabled={themeSwitching}
            onClick={toggleTheme}
          >
            <span className={`settings__appearance-option${themeMode === 'light' ? ' settings__appearance-option--active' : ''}`} aria-hidden="true">
              <Sun width={17} height={17} />
            </span>
            <span className={`settings__appearance-option${themeMode === 'dark' ? ' settings__appearance-option--active' : ''}`} aria-hidden="true">
              <Moon width={17} height={17} />
            </span>
          </button>}
        </header>
        {themeError && <Alert color="danger" variant="soft" description={themeError} />}

        {!selectedProvider && !mobiusAccountOpen && <section className="settings__section settings__section--accounts" aria-labelledby="settings-accounts-title">
          <h2 className="settings__section-title" id="settings-accounts-title">Accounts</h2>
          <div className="settings__providers">
            <button type="button" className="settings__account-link" onClick={openMobiusYou}>
              <span className="settings__account-icon settings__account-icon--profile" aria-hidden="true">
                <ProfileAvatar profile={mobiusProfile} token={getToken()} />
              </span>
              <span className="settings__account-link-copy"><strong>Möbius account</strong><small>{mobiusProfile?.handle ? `@${mobiusProfile.handle} · ` : ''}Profile, hosting and deployments</small></span>
              <span className="settings__account-link-arrow" aria-hidden="true">›</span>
            </button>
            <GithubConnection
              active={active}
              expanded={githubAccountOpen}
              onToggle={toggleGithub}
              onExpand={expandGithub}
              focusRef={(node) => setSetupFocusRef('github', node)}
              attention={attentionSection === 'github'}
            />
          </div>
        </section>}

        {mobiusAccountOpen && <div className="settings__account-page">
          <IdentityAccount token={getToken()} />
        </div>}

        {!mobiusAccountOpen && <section
          className={`settings__section settings__section--ai${attentionSection === 'ai-providers' ? ' settings-setup-target' : ''}`}
          id="settings-ai-providers"
          ref={(node) => setSetupFocusRef('ai-providers', node)}
          tabIndex={-1}
        >
          {!selectedProvider && <>
            <h2 className="settings__section-title">AI providers</h2>
            <p className="settings__subtext settings__subtext--tight">Drag connected providers to set background-task priority. Disconnected providers stay below. New chats still use your last-picked chat model.</p>
          </>}
          {providerReady ? (
            <div
              className={`settings-bg-list${selectedProvider ? ' settings-bg-list--detail' : ''}${backgroundCommitting ? ' settings-bg-list--committing' : ''}`}
              id="settings-background-agents"
              ref={(node) => setSetupFocusRef('background-agents', node)}
              tabIndex={-1}
            >
              {effectiveBackgroundDraft.map((row, index) => (selectedProvider && selectedProvider !== row.provider ? null : (
                  <BackgroundProviderRow
                    key={row.provider}
                    row={row}
                    providerInfo={modelProviderInfo[row.provider]}
                    index={index}
                    models={modelsForProvider(row.provider)}
                    dragging={backgroundDrag?.fromIndex === index}
                    dropTarget={
                      backgroundDrag?.toIndex === index
                      && backgroundDrag?.fromIndex !== index
                    }
                    dragStyle={backgroundDragStyleForIndex(index)}
                    reorderMode={!selectedProvider}
                    rowRef={(node) => {
                      backgroundRowRefs.current[index] = node
                    }}
                    onModelChange={(model, effort) => setBackgroundProviderChoice(row.provider, {
                      enabled: !!model,
                      model: model || defaultBackgroundModel(row.provider),
                      ...(effort ? { effort } : {}),
                    })}
                    onEffortChange={(effort) => setBackgroundProviderChoice(row.provider, { effort })}
                    onMove={(delta) => {
                      // Keyboard reorder is disabled while a pointer drag
                      // is live, so the two reorder paths can't interleave
                      // and mutate the list from under each other.
                      if (backgroundDrag) return
                      moveBackgroundProvider(index, index + delta)
                    }}
                    configuredProviders={configuredProviders}
                    onReorderStart={startBackgroundReorder}
                    detailMode={selectedProvider === row.provider}
                    onOpen={() => { setSelectedProvider(row.provider); settingsBoundaryRef.current?.scrollTo({ top: 0 }) }}
                    details={<>
                      <button type="button" className="settings-provider-details__models" onClick={() => setManageModelsProvider(row.provider)} disabled={!configuredProviders.has(row.provider)}>
                        Manage chat models
                      </button>
                      {row.provider === 'codex' && (
                <ProviderRow
                  name="OpenAI Codex"
                  connected={codexAuthenticated}
                  actionLabel={codexAuthenticated ? 'Manage' : 'Connect'}
                  version={codexVersion}
                  statusNode={codexAuthenticated ? (
                    <span className="provider-plan-label">{formatPlanStatus(codexPlanLabel)}</span>
                  ) : undefined}
                  detailNode={codexAuthenticated ? (
                    <ProviderUsage
                      id="provider-usage-codex"
                      snapshot={codexUsageQuery.data}
                      loading={codexUsageQuery.isPending}
                      failed={codexUsageQuery.isError}
                      refreshing={codexUsageQuery.isFetching}
                      onRefresh={() => codexUsageQuery.refetch()}
                      onRedeemReset={handleRedeemCodexReset}
                      redeeming={codexRedeem.busy}
                      redeemResult={codexRedeem.result}
                    />
                  ) : null}
                  expanded={expandedAuth === 'codex'}
                  onToggleExpand={toggleCodexAuth}
                >
                  <ProviderConnection provider="codex" name="OpenAI Codex" connected={codexAuthenticated} onDisconnected={onProviderDisconnected}>
                    <CodexAuth onConnected={onCodexAuthDone} />
                  </ProviderConnection>
                </ProviderRow>
                      )}
                      {row.provider === 'claude' && (
                <ProviderRow
                  name="Claude Code"
                  connected={claudeAuthenticated}
                  actionLabel={claudeAuthenticated ? 'Manage' : 'Connect'}
                  version={claudeVersion}
                  statusNode={claudeAuthenticated ? (
                    <span className="provider-plan-label">{formatPlanStatus(claudePlanLabel)}</span>
                  ) : undefined}
                  detailNode={claudeAuthenticated ? (
                    <ProviderUsage
                      id="provider-usage-claude"
                      snapshot={claudeUsageQuery.data}
                      loading={claudeUsageQuery.isPending}
                      failed={claudeUsageQuery.isError}
                      refreshing={claudeUsageQuery.isFetching}
                      onRefresh={() => claudeUsageQuery.refetch()}
                      onRedeemClaudeReset={handleRedeemClaudeReset}
                      claudeResetRedeeming={claudeResetRedeem.busy}
                      claudeResetResult={claudeResetRedeem.result}
                      onToggleExtraUsage={handleClaudeExtraUsage}
                      extraUsageBusy={claudeExtraUsage.busy}
                      extraUsageResult={claudeExtraUsage.result}
                    />
                  ) : null}
                  expanded={expandedAuth === 'claude'}
                  onToggleExpand={toggleClaudeAuth}
                >
                  <ProviderConnection provider="claude" name="Claude Code" connected={claudeAuthenticated} onDisconnected={onProviderDisconnected}>
                    <ProviderAuth
                      authenticated={claudeAuthenticated}
                      compact
                      onDone={onClaudeAuthDone}
                    />
                  </ProviderConnection>
                </ProviderRow>
                      )}
                      {row.provider === 'mobius' &&
                        <MobiusProviderAccess token={getToken()} onVisibilityChange={() => {
                          authQueries.provider.statuses.invalidate(queryClient)
                          modelQueries.registry.invalidate(queryClient)
                        }} />}
                      {(row.provider === 'claude' || row.provider === 'codex') && (
                        <div className="settings-provider-details__pricing">
                          <span>Token rates are a reference, not your account bill. Subscription usage may be included.</span>
                          <a href={row.provider === 'claude' ? 'https://www.anthropic.com/pricing' : 'https://platform.openai.com/pricing'} target="_blank" rel="noopener noreferrer">See current rates ↗</a>
                        </div>
                      )}
                    </>}
                  />
              )))}
            </div>
          ) : providerError ? (
            <Alert color="danger" variant="soft" description={providerErrorMsg} actions={<button className="settings__btn settings__btn--outline settings__btn--sm" type="button" onClick={retryProviders}>Retry</button>} />
          ) : (
            <div className="settings__notice" role="status">Loading providers…</div>
          )}
          {backgroundError && <Alert color="info" variant="soft" description={backgroundError} />}
          {manageModelsProvider && (
            <ManageModelsModal
              onClose={() => setManageModelsProvider(null)}
              providerOrder={modelProviderOrder}
              providerInfo={modelProviderInfo}
              configuredProviders={configuredProviders}
              onlyProvider={manageModelsProvider === 'all' ? null : manageModelsProvider}
            />
          )}
        </section>}

        {!selectedProvider && !mobiusAccountOpen && <>
        <PlatformUpdates active={active} refreshToken={refreshToken} onOpenChat={onOpenChat} inertBoundaryRef={settingsBoundaryRef} />

        <section className="settings__section settings__section--compact">
          <div className="settings__row">
            <span className="settings__label">Session</span>
            {signOutConfirm ? (
              <div className="settings__confirm">
                <button
                  className="settings__btn settings__btn--outline settings__btn--sm"
                  type="button"
                  onClick={() => setSignOutConfirm(false)}
                  disabled={signingOut}
                >
                  Cancel
                </button>
                <button
                  className="settings__btn settings__btn--sm settings__btn--nowrap"
                  type="button"
                  onClick={signOut}
                  disabled={signingOut}
                >
                  {signingOut ? 'Leaving…' : onLeaveSharedAccess ? 'Leave shared access' : 'Sign out'}
                </button>
              </div>
            ) : (
              <button
                className="settings__btn settings__btn--outline settings__btn--sm"
                type="button"
                onClick={() => setSignOutConfirm(true)}
              >
                {onLeaveSharedAccess ? 'Leave shared access' : 'Sign out'}
              </button>
            )}
          </div>
          {signOutConfirm && !signingOut && (
            <p className="settings__subtext settings__subtext--tight">
              This clears chats, drafts, and app sessions cached on this device.
            </p>
          )}
        </section>
        </>}
      </div>
    </div>
  )
}
