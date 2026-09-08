import { useState, useEffect, useCallback, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { ChevronDown, Moon, Sun } from '@openai/apps-sdk-ui/components/Icon'
import GripVertical from 'lucide-react/dist/esm/icons/grip-vertical.mjs'
import { api, clearQueryCache, clearToken } from '../../api/client.js'
import { authQueries, modelQueries, settingsQueries, themeQueries } from '../../hooks/queries.js'
import { settleBackgroundAgentSave } from '../../lib/backgroundAgentSave.js'
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
import StatusDot from '../ui/StatusDot.jsx'
import ModelSheet from '../ui/ModelSheet.jsx'
import { modelEfforts, validEffort } from '../ui/modelEfforts.js'
import ManageModelsModal from '../ChatView/ManageModelsModal.jsx'
import PlatformUpdates from './PlatformUpdates.jsx'
import ProviderUsage from './ProviderUsage.jsx'
import {
  formatPlanStatus,
  formatTrialTimeLeft,
  providerAllowance,
  providerAllowanceSummary,
} from './providerUsage.js'
import { PROVIDER_INFO, PROVIDER_ORDER } from '../ChatView/ChatSettingsPanel.jsx'
import '../ui/StatusDot.css'
import '../ui/ModelSheet.css'
import './SettingsView.css'

const PROVIDER_CHOICES = [
  { id: 'mobius', label: 'Möbius subscription' },
  { id: 'claude', label: 'Claude Code' },
  { id: 'codex', label: 'OpenAI Codex' },
]
const DEFAULT_BACKGROUND_MODELS = {
  mobius: 'inkling',
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
  return PROVIDER_CHOICES.some(p => p.id === provider)
}

function PlanUsageToggle({ provider, label, expanded, onToggle }) {
  return (
    <button
      type="button"
      className="provider-plan-toggle"
      onClick={onToggle}
      aria-expanded={expanded}
      aria-controls={`provider-usage-${provider}`}
      aria-label={`${label}, ${expanded ? 'hide' : 'show'} usage`}
    >
      <span>{label}</span>
      <ChevronDown
        className="provider-plan-toggle__chevron"
        width={13}
        height={13}
        aria-hidden="true"
      />
    </button>
  )
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
  } else {
    addChoice(
      backgroundAgents?.primary || { provider: resolvedDefaultProvider },
      true,
    )
    addChoice(backgroundAgents?.fallback, true)
  }

  if (!rows.length) addChoice({ provider: resolvedDefaultProvider }, true)
  PROVIDER_ORDER.forEach((provider) => addChoice({ provider }, false))
  if (!rows.some(row => row.enabled)) rows[0].enabled = true
  return rows
}

function BackgroundProviderRow({
  row,
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
}) {
  const info = PROVIDER_INFO[row.provider]
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
        + (enabled ? '' : ' settings-bg-row--off')
        + (configured ? '' : ' settings-bg-row--disconnected')
        + (reorderMode ? ' settings-bg-row--reordering' : '')
        + (dragging ? ' settings-bg-row--dragging' : '')
        + (dropTarget ? ' settings-bg-row--drop-target' : '')
      }
      style={dragStyle}
      aria-label={`${info?.label || row.provider} background priority ${index + 1}`}
    >
      {reorderMode && (
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
          disabled={!configured}
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
      <div className="settings-bg-row__body">
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
}) {
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
  // Usage is deliberately on demand. Only the disclosed provider fetches,
  // and keeping this separate from auth preserves the row's two clear actions.
  const [expandedUsage, setExpandedUsage] = useState({
    codex: false,
    claude: false,
  })
  // Surface failures from the dark-mode toggle: a failed theme
  // persist would otherwise bounce the knob without telling the user
  // why.
  const [themeError, setThemeError] = useState('')
  const [signOutConfirm, setSignOutConfirm] = useState(false)
  const [signingOut, setSigningOut] = useState(false)

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
  const mobiusAvailable = providerStatusQuery.data?.mobius?.available === true
  const mobiusAuthenticated = configuredProviders.has('mobius')
  const mobiusTrial = providerStatusQuery.data?.mobius?.trial
  const mobiusExpiryRaw = mobiusTrial?.trial_expires_at
    || mobiusTrial?.account?.trial_expires_at
    || mobiusTrial?.balance?.grants?.find(grant => grant?.kind === 'trial')?.expires_at
  const mobiusExpiryTime = Date.parse(mobiusExpiryRaw || '')
  const mobiusHasExpiry = Number.isFinite(mobiusExpiryTime)
  const mobiusExpired = mobiusHasExpiry && mobiusExpiryTime <= Date.now()
  // Live-probed CLI versions (null when the CLI isn't installed or
  // didn't respond). Read-only — updates happen via the agent, not here.
  const claudeVersion = settingsQuery.data?.claude_version
  const codexVersion = settingsQuery.data?.codex_version
  const claudeAuthenticated = configuredProviders.has('claude')
  const hasConfiguredProvider = configuredProviders.size > 0
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
      && expandedUsage.codex
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
      && expandedUsage.claude
    ),
  })
  const mobiusUsageQuery = settingsQueries.providerUsage.useQuery('mobius', {
    enabled: active && providerReady && mobiusAvailable && mobiusAuthenticated,
  })
  const mobiusAllowance = providerAllowance('mobius', mobiusUsageQuery.data)
  const mobiusTrialSubtitle = mobiusAuthenticated
    ? (
        mobiusExpired
          ? 'Trial expired'
          : (
              typeof mobiusAllowance.usedPercent === 'number'
                ? providerAllowanceSummary('mobius', mobiusAllowance)
                : formatTrialTimeLeft(mobiusExpiryRaw) || 'Trial usage unavailable'
            )
      )
    : 'Sign in from Möbius · You to activate your trial.'
  // Registry and provider/settings probes are independent. Starting them
  // together avoids an unnecessary request waterfall on a first open.
  const modelRegistryQuery = modelQueries.registry.useQuery()
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
  const [manageModelsOpen, setManageModelsOpen] = useState(false)
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
    if (requested === 'models') setManageModelsOpen(true)
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
          primary: toChoice(enabled[0]),
          fallback: enabled[1] ? toChoice(enabled[1]) : null,
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
    const current = backgroundDraftRef.current ||
      normalizeBackgroundAgents(
        settingsQuery.data?.background_agents,
        providerFromSettings(settingsQuery.data),
      )
    const next = typeof updater === 'function' ? updater(current) : updater
    backgroundDraftRef.current = next
    setBackgroundDraft(next)
    persistBackgroundAgents(next)
  }, [persistBackgroundAgents, settingsQuery.data])

  const setBackgroundProviderChoice = useCallback((provider, patch) => {
    updateBackgroundDraft((current) => current.map((row) => {
      if (row.provider !== provider) return row
      return { ...row, ...patch }
    }))
  }, [updateBackgroundDraft])

  const moveBackgroundProvider = useCallback((fromIndex, toIndex) => {
    const total = (backgroundDraftRef.current ||
      normalizeBackgroundAgents(
        settingsQuery.data?.background_agents,
        providerFromSettings(settingsQuery.data),
      )).length
    if (toIndex < 0 || toIndex >= total || fromIndex === toIndex) return
    updateBackgroundDraft((current) => {
      const next = [...current]
      const [row] = next.splice(fromIndex, 1)
      next.splice(toIndex, 0, row)
      return next
    })
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
    const node = backgroundRowRefs.current[index] || pointer?.node
    if (!node) return
    const layoutSpace = captureLayoutSpace(node)
    const toLayoutY = clientY => clientLengthToLayout(
      clientY - layoutSpace.clientTop,
      layoutSpace,
    )
    const rowRect = node.getBoundingClientRect()
    const rows = backgroundRowRefs.current
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

  // Stable identity-preserving callbacks: passing fresh arrow
  // functions in JSX re-mounted ProviderRow's event handlers every
  // render, which combined with the row's CSS transitions made the
  // panel feel jittery. With the updater form, deps are empty.
  const toggleClaudeAuth = useCallback(
    () => {
      setExpandedUsage(prev => (
        prev.claude ? { ...prev, claude: false } : prev
      ))
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
      setExpandedUsage(prev => (
        prev.codex ? { ...prev, codex: false } : prev
      ))
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
    onOpenApp?.('identity')
  }, [onOpenApp])
  const toggleClaudeUsage = useCallback(() => {
    setExpandedAuth(prev => prev === 'claude' ? null : prev)
    setExpandedUsage(prev => ({ ...prev, claude: !prev.claude }))
  }, [])
  const toggleCodexUsage = useCallback(() => {
    setExpandedAuth(prev => prev === 'codex' ? null : prev)
    setExpandedUsage(prev => ({ ...prev, codex: !prev.codex }))
  }, [])
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
      backgroundDraftRef.current = next
      setBackgroundDraft(next)
      const saved = await persistBackgroundAgents(
        next,
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
    try {
      await themeService.toggleTheme(queryClient, currentMode, api)
    } catch {
      setThemeMode(currentMode)
      setThemeError(
        'Could not save theme. Check your connection and try again.',
      )
      // Force the mode query to resync with the server. Covers the
      // write-succeeded-but-response-lost case: refetching reads
      // authoritative state, the mirror effect at line 30 picks it
      // up, and themeMode stops disagreeing with the visible theme.
      themeQueries.mode.invalidate(queryClient)
      onThemeChange?.()  // reload original theme on error
    } finally {
      setThemeSwitching(false)
    }
  }

  async function signOut() {
    if (signingOut) return
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

  const effectiveBackgroundDraft = backgroundDraft ||
    normalizeBackgroundAgents(
      settingsQuery.data?.background_agents,
      providerFromSettings(settingsQuery.data),
    )
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
  // The chat model row's status line shows the current default rather
  // than a connection dot ("Last model: Opus 4.8"). Resolve the label
  // from the live registry so it reads the friendly name, falling back
  // to the raw id, then to nothing when no default is set yet.
  const defaultChatProvider = providerFromSettings(settingsQuery.data)
  const defaultChatModelId = settingsQuery.data?.agent_settings?.model || ''
  const lastModelLabel = defaultChatModelId
    ? (modelsForProvider(defaultChatProvider).find(m => m.id === defaultChatModelId)?.label
        || defaultChatModelId)
    : ''
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
    <div className="settings">
      <div className="settings__content">
        <h1 className="settings__title">Settings</h1>

        <section
          className={`settings__section${attentionSection === 'ai-providers' ? ' settings-setup-target' : ''}`}
          id="settings-ai-providers"
          ref={(node) => setSetupFocusRef('ai-providers', node)}
          tabIndex={-1}
        >
          <h2 className="settings__section-title">AI providers</h2>

          {providerReady ? (
            <>
              <div className="settings__providers">
                <ProviderRow
                  name="OpenAI Codex"
                  connected={codexAuthenticated}
                  actionLabel={codexAuthenticated ? 'Manage' : 'Connect'}
                  version={codexVersion}
                  statusNode={codexAuthenticated ? (
                    <PlanUsageToggle
                      provider="codex"
                      label={formatPlanStatus(codexPlanLabel)}
                      expanded={expandedUsage.codex}
                      onToggle={toggleCodexUsage}
                    />
                  ) : undefined}
                  detailNode={codexAuthenticated && expandedUsage.codex ? (
                    <ProviderUsage
                      id="provider-usage-codex"
                      snapshot={codexUsageQuery.data}
                      loading={codexUsageQuery.isPending}
                      failed={codexUsageQuery.isError}
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

                <ProviderRow
                  name="Claude Code"
                  connected={claudeAuthenticated}
                  actionLabel={claudeAuthenticated ? 'Manage' : 'Connect'}
                  version={claudeVersion}
                  statusNode={claudeAuthenticated ? (
                    <PlanUsageToggle
                      provider="claude"
                      label={formatPlanStatus(claudePlanLabel)}
                      expanded={expandedUsage.claude}
                      onToggle={toggleClaudeUsage}
                    />
                  ) : undefined}
                  detailNode={claudeAuthenticated && expandedUsage.claude ? (
                    <ProviderUsage
                      id="provider-usage-claude"
                      snapshot={claudeUsageQuery.data}
                      loading={claudeUsageQuery.isPending}
                      failed={claudeUsageQuery.isError}
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

                {mobiusAvailable && (
                  <ProviderRow
                    name="Möbius subscription"
                    connected={mobiusAuthenticated}
                    subtitle={mobiusTrialSubtitle}
                    statusNode={(
                      <StatusDot color={mobiusAuthenticated && !mobiusExpired ? '--green' : '--muted'}>
                        {mobiusAuthenticated
                          ? (mobiusExpired ? 'Trial expired' : 'Trial active')
                          : 'Sign in from Möbius · You'}
                      </StatusDot>
                    )}
                    expanded={false}
                    actionLabel="Open Möbius · You"
                    onToggleExpand={openMobiusYou}
                  />
                )}

                <ProviderRow
                  name="Chat model"
                  connected={hasConfiguredProvider}
                  disabled={!hasConfiguredProvider}
                  subtitle={hasConfiguredProvider
                    ? 'Choose which models appear. New chats use your last pick.'
                    : 'Connect an AI provider to choose chat models.'}
                  statusNode={
                    <span className="provider-row__status-text settings__last-model">
                      {!hasConfiguredProvider ? 'No provider connected' : lastModelLabel ? (
                        <>
                          Last model: <span className="settings__standard-highlight">{lastModelLabel}</span>
                        </>
                      ) : 'No default yet'}
                    </span>
                  }
                  actionLabel="Configure"
                  expanded={false}
                  onToggleExpand={() => setManageModelsOpen(true)}
                />
              </div>

              <div
                className={
                  `settings-agent-group${hasConfiguredProvider ? '' : ' settings-agent-group--disabled'}`
                  + (attentionSection === 'background-agents' ? ' settings-setup-target' : '')
                }
                id="settings-background-agents"
                ref={(node) => setSetupFocusRef('background-agents', node)}
                tabIndex={-1}
              >
                <div className="settings-agent-group__head">
                  <div className="settings-agent-group__title-row">
                    <h3 className="settings__agent-title">Background agents</h3>
                  </div>
                  <p className="settings__subtext settings__subtext--tight">
                    {hasConfiguredProvider
                      ? 'Used for memory, reflection, and other automatic tasks. Tried in order.'
                      : 'Connect an AI provider to configure automatic tasks.'}
                  </p>
                </div>
                <div
                  className={`settings-bg-list${backgroundCommitting ? ' settings-bg-list--committing' : ''}`}
                >
                  {effectiveBackgroundDraft.map((row, index) => (
                    <BackgroundProviderRow
                      key={row.provider}
                      row={row}
                      index={index}
                      models={modelsForProvider(row.provider)}
                      dragging={backgroundDrag?.fromIndex === index}
                      dropTarget={
                        backgroundDrag?.toIndex === index
                        && backgroundDrag?.fromIndex !== index
                      }
                      dragStyle={backgroundDragStyleForIndex(index)}
                      reorderMode
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
                    />
                  ))}
                </div>
                {backgroundError && (
                  <Alert
                    color="info"
                    variant="soft"
                    description={backgroundError}
                  />
                )}
              </div>
              {manageModelsOpen && (
                <ManageModelsModal
                  onClose={() => setManageModelsOpen(false)}
                  providerOrder={PROVIDER_ORDER}
                  providerInfo={PROVIDER_INFO}
                  configuredProviders={configuredProviders}
                />
              )}
            </>
          ) : providerError ? (
            // First-ever open with no persisted cache and the fetch
            // failed. Surface the error + a retry rather than rendering
            // the section blank — a silent empty section reads as "no
            // providers", which is wrong.
            <Alert
              color="danger"
              variant="soft"
              description={providerErrorMsg}
              actions={
                <button
                  className="settings__btn settings__btn--outline settings__btn--sm"
                  type="button"
                  onClick={retryProviders}
                >
                  Retry
                </button>
              }
            />
          ) : (
            // Loading: no cached data yet and no error — the initial
            // in-flight fetch. Show a neutral notice instead of nothing.
            <div className="settings__notice" role="status">
              Loading providers…
            </div>
          )}
        </section>

        <section className="settings__section settings__section--compact settings__section--appearance">
          <div className="settings__appearance">
            <span className="settings__label">Appearance</span>
            <button
              type="button"
              className="settings__appearance-toggle"
              role="switch"
              aria-label="Dark mode"
              aria-checked={themeMode === 'dark'}
              aria-busy={themeSwitching}
              disabled={themeSwitching}
              onClick={toggleTheme}
            >
              <span
                className={`settings__appearance-option${themeMode === 'light' ? ' settings__appearance-option--active' : ''}`}
                aria-hidden="true"
              >
                <Sun width={17} height={17} />
              </span>
              <span
                className={`settings__appearance-option${themeMode === 'dark' ? ' settings__appearance-option--active' : ''}`}
                aria-hidden="true"
              >
                <Moon width={17} height={17} />
              </span>
            </button>
          </div>
          {themeError && (
            <Alert
              color="danger"
              variant="soft"
              description={themeError}
            />
          )}
        </section>

        <PlatformUpdates active={active} refreshToken={refreshToken} onOpenChat={onOpenChat} />

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
                  {signingOut ? 'Signing out…' : 'Sign out'}
                </button>
              </div>
            ) : (
              <button
                className="settings__btn settings__btn--outline settings__btn--sm"
                type="button"
                onClick={() => setSignOutConfirm(true)}
              >
                Sign out
              </button>
            )}
          </div>
          {signOutConfirm && !signingOut && (
            <p className="settings__subtext settings__subtext--tight">
              This clears chats, drafts, and app sessions cached on this device.
            </p>
          )}
        </section>
      </div>
    </div>
  )
}
