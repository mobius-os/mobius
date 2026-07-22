import { useState, useEffect, useCallback, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import GripVertical from 'lucide-react/dist/esm/icons/grip-vertical.mjs'
import Moon from 'lucide-react/dist/esm/icons/moon.mjs'
import Sun from 'lucide-react/dist/esm/icons/sun.mjs'
import { api, clearQueryCache, clearToken } from '../../api/client.js'
import { authQueries, modelQueries, settingsQueries, themeQueries, versionQueries } from '../../hooks/queries.js'
import { platformVersionIdentity } from '../../lib/platformVersionIdentity.js'
import {
  PROVIDER_AVAILABILITY_PHASE,
  resolveProviderAvailability,
} from '../../lib/providerAvailability.js'
import { restartCanReload } from '../../lib/restartReadiness.js'
import { updateCheckOutcome, updateCheckLabel } from '../../lib/updateCheckPhase.js'
import * as themeService from '../../lib/themeService.js'
import { CLAUDE_MODELS, CODEX_MODELS } from '../ProviderModelPicker/ProviderModelPicker.jsx'
import ProviderAuth from '../ProviderAuth/ProviderAuth.jsx'
import CodexAuth from '../ProviderAuth/CodexAuth.jsx'
import ProviderRow from '../ProviderAuth/ProviderRow.jsx'
import StatusDot from '../ui/StatusDot.jsx'
import ModelSheet from '../ui/ModelSheet.jsx'
import { modelEfforts, validEffort } from '../ui/modelEfforts.js'
import ManageModelsModal from '../ChatView/ManageModelsModal.jsx'
import UpdateReviewModal from './UpdateReviewModal.jsx'
import { PROVIDER_INFO, PROVIDER_ORDER } from '../ChatView/ChatSettingsPanel.jsx'
import '../ui/StatusDot.css'
import '../ui/ModelSheet.css'
import './SettingsView.css'

// Order a provider's models with the currently-selected one first,
// then the rest in registry order (which the backend returns newest →
// oldest / most → least capable). Floating the active model to the top
// of its group is what makes scrolling the picker feel natural — the
// choice you reach for is always the first thing under your thumb.
function orderSelectedFirst(models, selectedId) {
  if (!Array.isArray(models) || !selectedId) return models || []
  const sel = models.find((m) => m.id === selectedId)
  if (!sel) return models
  return [sel, ...models.filter((m) => m.id !== selectedId)]
}

const UPDATE_CHECKED_RESET_MS = 2200
const RETURN_VIEW_KEY = 'mobius:return-view'
const RESTART_POLL_INTERVAL_MS = 1500
const RESTART_POLL_MAX = 40
const RESTART_SHELL_READY_PATH = '/shell/'
const PLATFORM_APPLY_STATES = new Set([
  'restart_needed', 'up_to_date', 'conflict', 'rolled_back',
])
const PROVIDER_CHOICES = [
  { id: 'claude', label: 'Claude Code' },
  { id: 'codex', label: 'OpenAI Codex' },
]
const FALLBACK_MODEL_ROWS = {
  claude: CLAUDE_MODELS.map((m) => ({ id: m.value, label: m.label, available: true })),
  codex: CODEX_MODELS.map((m) => ({ id: m.value, label: m.label, available: true })),
}
const DEFAULT_BACKGROUND_MODELS = {
  claude: 'claude-opus-4-8',
  codex: 'gpt-5.6-terra',
}

// POST /platform/apply is the authoritative outcome of the mutation it just
// performed. Project that result into the status shape immediately so a failed
// follow-up GET /status cannot leave Settings claiming the old state. A fresh
// successful status response still replaces this fallback in refreshPlatform.
function platformStatusFromApply(previous, result) {
  const state = result.state
  const upstream = result.upstream_commit || previous?.recorded_upstream_sha || null
  const clean = state === 'restart_needed' || state === 'up_to_date'
  return {
    ...(previous || {}),
    state,
    available: state === 'rolled_back',
    needs_restart: state === 'restart_needed' || !!result.needs_restart,
    current_build_sha: previous?.current_build_sha || null,
    recorded_upstream_sha: upstream,
    contained_upstream_sha: clean
      ? (result.upstream_commit || previous?.contained_upstream_sha || null)
      : (previous?.contained_upstream_sha || null),
    seed_required: false,
    conflict_paths: Array.isArray(result.conflict_paths)
      ? result.conflict_paths
      : [],
    conflict_chat_id: state === 'conflict' ? (result.chat_id || null) : null,
  }
}

function defaultEffort(provider) {
  const efforts = PROVIDER_INFO[provider]?.efforts || []
  return efforts.find(e => e.value === 'medium')?.value || efforts[0]?.value || ''
}

function defaultModel(provider) {
  return FALLBACK_MODEL_ROWS[provider]?.[0]?.id || ''
}

function defaultBackgroundModel(provider) {
  return DEFAULT_BACKGROUND_MODELS[provider] || defaultModel(provider)
}

function isKnownProvider(provider) {
  return PROVIDER_CHOICES.some(p => p.id === provider)
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
  const enabled = row.enabled !== false
  const selectedModel = enabled ? (row.model || defaultModel(row.provider)) : ''
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
    models: orderSelectedFirst(models, enabled ? selectedModel : null),
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
          <GripVertical size={18} strokeWidth={2} aria-hidden="true" />
        </button>
      )}
      <div className="settings-bg-row__body">
        <button
          type="button"
          className={`model-trigger${enabled ? '' : ' model-trigger--off'}`}
          onClick={() => setSheetOpen(true)}
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

function returnToSettingsAfterReload() {
  try { sessionStorage.setItem(RETURN_VIEW_KEY, 'settings') } catch {}
}

async function readRestartHealth() {
  const response = await fetch('/api/health', {
    cache: 'no-store',
    credentials: 'same-origin',
  })
  if (!response.ok) return { ok: false, bootId: '' }
  let body = null
  try { body = await response.json() } catch {}
  return {
    ok: true,
    bootId: typeof body?.boot_id === 'string' ? body.boot_id : '',
  }
}

async function readRestartBootId() {
  try {
    const health = await readRestartHealth()
    return health.ok ? health.bootId : ''
  } catch {
    return ''
  }
}

async function shellDocumentReady() {
  if (typeof window === 'undefined') return false
  try {
    const response = await fetch(new URL(RESTART_SHELL_READY_PATH, window.location.origin), {
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { Accept: 'text/html' },
    })
    if (!response.ok) return false
    return (response.headers.get('content-type') || '').toLowerCase().includes('text/html')
  } catch {
    return false
  }
}

export default function SettingsView({ onThemeChange, onOpenChat, focusTarget = null }) {
  const queryClient = useQueryClient()
  const settingsQuery = settingsQueries.owner.useQuery()
  const providerStatusQuery = authQueries.provider.statuses.useQuery()
  const themeModeQuery = themeQueries.mode.useQuery()
  const versionQuery = versionQueries.current.useQuery()
  const [themeMode, setThemeMode] = useState(() => (
    typeof document !== 'undefined'
    && document.documentElement.getAttribute('data-theme') === 'light'
      ? 'light'
      : 'dark'
  ))
  const [themeSwitching, setThemeSwitching] = useState(false)
  // Which provider has its inline auth panel expanded. null = none.
  const [expandedAuth, setExpandedAuth] = useState(null)
  // Surface failures from the dark-mode toggle: a failed theme
  // persist would otherwise bounce the knob without telling the user
  // why.
  const [themeError, setThemeError] = useState('')
  const [restartPhase, setRestartPhase] = useState('idle')
  const [restartError, setRestartError] = useState('')
  // A manual restart interrupts any live chat, so it's a deliberate two-step:
  // the first tap arms the confirm, the second actually restarts.
  const [restartConfirm, setRestartConfirm] = useState(false)
  const [signOutConfirm, setSignOutConfirm] = useState(false)
  const [signingOut, setSigningOut] = useState(false)
  // Platform self-update (backend/frontend/libraries/recovery as one release).
  // 'idle' | 'applying' | 'resolving' | 'restarting'.
  const [platform, setPlatform] = useState(null)
  const [platformPhase, setPlatformPhase] = useState('idle')
  const [platformError, setPlatformError] = useState('')
  // Whether the update-review sheet is open. The "Update" button routes through
  // it so the owner reviews the incoming changes before applying, rather than
  // Apply firing on the first click.
  const [reviewOpen, setReviewOpen] = useState(false)
  // Every update-row action occupies the same conditional slot. Keep one ref
  // on that slot so closing the review can focus the live replacement button
  // after refreshPlatform swaps Review for Restart/Resolve/Check.
  const platformActionRef = useRef(null)
  // 'idle' | 'checking' | 'checked' | 'error' — the "Check for updates" button
  // asks the service worker to re-check cached frontend assets and re-reads
  // /api/version. 'checked' is a short-lived success label when no update is
  // available; 'error' means a probe failed, so we say so instead of falsely
  // claiming "No updates found". 'error' persists (no auto-reset) until the
  // owner clicks the button again to retry.
  const [updatePhase, setUpdatePhase] = useState('idle')
  useEffect(() => {
    if (updatePhase !== 'checked') return undefined
    const timer = window.setTimeout(() => setUpdatePhase('idle'), UPDATE_CHECKED_RESET_MS)
    return () => window.clearTimeout(timer)
  }, [updatePhase])

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
    return FALLBACK_MODEL_ROWS[provider] || []
  }, [modelRegistryQuery.data])

  const persistBackgroundAgents = useCallback((draft, companionSettings = {}) => {
    const rows = Array.isArray(draft) ? draft : []
    const enabled = rows.filter(row => row.enabled !== false)
    if (!enabled.length) {
      setBackgroundError('Choose at least one background model.')
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
        if (reqId !== backgroundSaveReqRef.current) return true
        if (!res.ok) {
          let detail = ''
          try { detail = (await res.json()).detail || '' } catch {}
          throw new Error(detail || 'Could not save background agents.')
        }
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

  const backgroundIndexFromY = useCallback((clientY, slots) => {
    const rows = slots || backgroundRowRefs.current
      .map((node) => {
        if (!node) return null
        const rect = node.getBoundingClientRect()
        return {
          top: rect.top,
          height: rect.height,
          center: rect.top + rect.height / 2,
        }
      })
      .filter(Boolean)
    if (!rows.length) return 0
    // Partition by the MIDPOINT between adjacent slot centers, not the
    // centers themselves: the dragged row's projected center starts on its
    // own slot center, so a center-based test would flip to the next slot
    // on the very first pixel of travel (and snap a full row on release).
    // Midpoints mean "drag past halfway to swap" — a tiny nudge eases back.
    for (let index = 0; index < rows.length - 1; index++) {
      const boundary = (rows[index].center + rows[index + 1].center) / 2
      if (clientY < boundary) return index
    }
    return rows.length - 1
  }, [])

  // Called by a row once its press-and-hold elapses. `pointer` carries
  // the captured hold position/id (the original pointerdown event is
  // long gone by the time the hold timer fires).
  const startBackgroundReorder = useCallback((index, pointer) => {
    const node = backgroundRowRefs.current[index] || pointer?.node
    if (!node) return
    const rowRect = node.getBoundingClientRect()
    const rows = backgroundRowRefs.current
    const slots = rows.map((rowNode) => {
      if (!rowNode) return null
      const rect = rowNode.getBoundingClientRect()
      return {
        top: rect.top,
        height: rect.height,
        center: rect.top + rect.height / 2,
      }
    }).filter(Boolean)
    const captureNode = pointer?.captureNode || node
    try { captureNode.setPointerCapture?.(pointer?.pointerId) } catch { /* best-effort */ }
    const grabY = typeof pointer?.clientY === 'number' ? pointer.clientY : rowRect.top
    backgroundPointerYRef.current = grabY
    const next = {
      fromIndex: index,
      toIndex: index,
      grabOffsetY: grabY - rowRect.top,
      rowHeight: rowRect.height,
      slots,
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
    const { fromIndex, grabOffsetY, rowHeight, slots } = start
    const originTop = slots[fromIndex]?.top ?? 0
    const minOffset = slots.length ? slots[0].top - originTop : 0
    const maxOffset = slots.length ? slots[slots.length - 1].top - originTop : 0
    const followOffset = (clientY) => {
      const raw = clientY - grabOffsetY - originTop
      return Math.max(minOffset, Math.min(maxOffset, raw))
    }

    const onPointerMove = (event) => {
      event.preventDefault()
      const current = backgroundDragRef.current
      if (!current) return
      backgroundPointerYRef.current = event.clientY
      // Move the held row imperatively so it tracks the finger 1:1
      // without re-rendering the whole panel every frame.
      const node = backgroundRowRefs.current[fromIndex]
      if (node) {
        node.style.transform = `translateY(${followOffset(event.clientY)}px) scale(1.02)`
      }
      // Only re-render (to slide the other rows aside) when the target
      // slot actually changes.
      const dragCenterY = event.clientY - grabOffsetY + rowHeight / 2
      const toIndex = backgroundIndexFromY(dragCenterY, slots)
      setBackgroundDrag((c) => (
        c && c.toIndex !== toIndex ? { ...c, toIndex } : c
      ))
    }

    const finish = (event) => {
      event.preventDefault()
      const current = backgroundDragRef.current
      if (!current) return
      backgroundPointerYRef.current = event.clientY
      const dragCenterY = event.clientY - grabOffsetY + rowHeight / 2
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
    () => setExpandedAuth(prev => {
      if (prev !== 'claude') {
        authProvidersAtStartRef.current = new Set(configuredProvidersRef.current)
      }
      return prev === 'claude' ? null : 'claude'
    }),
    [],
  )
  const toggleCodexAuth = useCallback(
    () => setExpandedAuth(prev => {
      if (prev !== 'codex') {
        authProvidersAtStartRef.current = new Set(configuredProvidersRef.current)
      }
      return prev === 'codex' ? null : 'codex'
    }),
    [],
  )
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
    setExpandedAuth(null)
  }, [configuredProviders, persistBackgroundAgents, queryClient, settingsQuery.data])
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

  // Ref to track the active health-poll interval so we can cancel it on
  // component unmount or on a second restart attempt (shouldn't happen —
  // the button is disabled while restarting, but belt-and-braces).
  const restartPollRef = useRef(null)
  const clearRestartPoll = useCallback(() => {
    if (!restartPollRef.current) return
    window.clearTimeout(restartPollRef.current)
    restartPollRef.current = null
  }, [])
  useEffect(() => {
    return () => clearRestartPoll()
  }, [clearRestartPoll])

  async function restartServer() {
    if (restartPhase === 'restarting') return
    setRestartPhase('restarting')
    setRestartError('')
    try {
      const previousBootId = await readRestartBootId()
      const res = await api.admin.restart()
      if (!res.ok) {
        let detail = ''
        try { detail = (await res.json()).detail || '' } catch {}
        throw new Error(detail || `Restart failed (${res.status})`)
      }
      returnToSettingsAfterReload()
      pollRestartThenReload({
        previousBootId,
        onTimeout: () => {
          setRestartError("Server hasn't come back yet — check the container.")
          setRestartPhase('idle')
          setRestartConfirm(false)
        },
      })
    } catch (err) {
      setRestartPhase('idle')
      setRestartConfirm(false)
      setRestartError(err.message || 'Restart request failed.')
    }
  }

  async function signOut() {
    if (signingOut) return
    setSigningOut(true)
    clearToken()
    await clearQueryCache()
    window.location.reload()
  }

  // Refresh both Möbius update signals on demand: the service worker cache and
  // platform git availability. `registration.update()` forces a fresh fetch of
  // /sw.js (served `no-cache`) and we re-read /api/version for the current
  // served identity. Platform: POST /platform/check runs the `git fetch` that the cheap
  // /status read deliberately skips, so a deploy that landed since boot becomes
  // visible without waiting for a reboot. Both run in parallel and neither
  // failing blocks the other (allSettled). If either probe rejects we land in
  // 'error' and say "Couldn't check for updates" rather than falsely reporting
  // "No updates found" — an honest failure the owner can retry with the same
  // button (see updateCheckOutcome).
  async function checkForUpdates() {
    if (updatePhase === 'checking') return
    setUpdatePhase('checking')
    const frontendP = (async () => {
      if ('serviceWorker' in navigator) {
        const reg = await navigator.serviceWorker.getRegistration()
        if (reg) await reg.update()
      }
      await versionQueries.current.invalidate(queryClient)
      // refetch resolves with an error result rather than rejecting, so a
      // failed version probe must be re-thrown for allSettled to see it —
      // same honesty rule as the platform arm below (feature 20).
      const versionResult = await versionQuery.refetch()
      if (versionResult.isError) {
        throw versionResult.error ?? new Error('version check failed')
      }
    })()
    const platformP = (async () => {
      const res = await api.platform.check()
      // An HTTP failure must REJECT, not resolve: allSettled treats a
      // resolved probe as success, so a swallowed !ok let updateCheckOutcome
      // report "No updates found" on a real 500 (feature 20).
      if (!res.ok) throw new Error(`platform check failed: ${res.status}`)
      setPlatform(await res.json())
    })()
    const results = await Promise.allSettled([frontendP, platformP])
    setUpdatePhase(updateCheckOutcome(results))
  }

  // Reload onto the FRESH service worker so the new precache — including the
  // content-hashed /mobius-runtime.js — serves the reloaded page. sw.js calls
  // skipWaiting() + clientsClaim(), so a newly-installed worker activates on its
  // own; we just wait for it to take control before reloading, instead of racing
  // a reload the OLD worker serves with the stale runtime (which leaves
  // window.mobius missing durableWrite/createUseDocument and breaks migrated
  // mini-apps until the tab happens to reload onto the new worker).
  async function reloadOntoFreshSW() {
    if ('serviceWorker' in navigator) {
      try {
        const reg = await navigator.serviceWorker.getRegistration()
        if (reg) {
          await reg.update()
          if (reg.installing || reg.waiting) {
            await new Promise((resolve) => {
              navigator.serviceWorker.addEventListener('controllerchange', resolve, { once: true })
              setTimeout(resolve, 5000) // never hang if the worker never swaps
            })
          }
        }
      } catch { /* fall through to a plain reload */ }
    }
    if (!(await shellDocumentReady())) return false
    window.location.reload()
    return true
  }

  // Platform self-update: read availability once on mount so Settings can show
  // "new update available" without a polling daemon.
  const refreshPlatform = useCallback(async () => {
    try {
      const res = await api.platform.status()
      if (res.ok) setPlatform(await res.json())
    } catch {
      // A status hiccup just leaves the section hidden — never blocks Settings.
    }
  }, [])
  useEffect(() => { refreshPlatform() }, [refreshPlatform])

  // Silent freshen on opening Settings: ask the SW for a newer cache manifest
  // and re-read /api/version so the Möbius row's served identity is current the
  // moment it renders. This is the cheap on-open pass (no git fetch) — it
  // complements the explicit "Check for updates" button, which additionally
  // fetches platform availability. Once per open; never touches updatePhase, so
  // it can't make the Update/Check button look busy.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        if ('serviceWorker' in navigator) {
          const reg = await navigator.serviceWorker.getRegistration()
          if (reg) await reg.update()
        }
        if (cancelled) return
        await versionQueries.current.invalidate(queryClient)
        await versionQuery.refetch()
      } catch {
        // a refresh hiccup just leaves the last-known status on the row
      }
    })()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Apply the reviewed update: merge the fetched platform release into the live
  // backend. Clean -> the row flips to "restart needed"; conflict -> show a
  // resolver action, but wait for the owner's click before opening an agent
  // chat. Returns the domain outcome (not merely HTTP success) so the review
  // sheet closes only for a clean apply and becomes an explicit result when
  // the update was blocked.
  async function applyPlatformUpdate() {
    if (platformPhase !== 'idle') return { ok: false }
    setPlatformError('')
    setPlatformPhase('applying')
    try {
      const res = await api.platform.apply()
      let body = null
      try { body = await res.json() } catch {}
      if (!res.ok) {
        const detail = body?.detail || ''
        setPlatformError(detail ? `Update failed: ${detail}` : 'Update failed — the instance is unchanged.')
        return { ok: false }
      }
      const state = typeof body?.state === 'string' ? body.state : ''
      if (PLATFORM_APPLY_STATES.has(state)) {
        setPlatform(current => platformStatusFromApply(current, body))
      }
      await refreshPlatform()
      if (state === 'restart_needed' || state === 'up_to_date') {
        return { ok: true, state }
      }
      if (state === 'conflict' || state === 'rolled_back') {
        return { ok: false, state }
      }
      setPlatformError(
        'The update returned an unexpected result. This review will stay open — check the current status before trying again.',
      )
      return { ok: false, state }
    } catch {
      setPlatformError('Update failed — the instance is unchanged.')
      return { ok: false }
    } finally {
      setPlatformPhase('idle')
    }
  }

  async function resolvePlatformConflict() {
    if (platformPhase !== 'idle' || !onOpenChat) return
    setPlatformError('')

    if (platform?.conflict_chat_id) {
      onOpenChat(platform.conflict_chat_id)
      return
    }

    setPlatformPhase('resolving')
    try {
      const res = await api.platform.conflictResolverChat()
      if (!res.ok) {
        let detail = ''
        try { detail = (await res.json())?.detail || '' } catch {}
        setPlatformError(detail ? `Could not open chat: ${detail}` : 'Could not open the resolver chat.')
        await refreshPlatform()
        return
      }
      const body = await res.json()
      await refreshPlatform()
      if (body?.chat_id) onOpenChat(body.chat_id)
    } catch {
      setPlatformError('Could not open the resolver chat.')
    } finally {
      setPlatformPhase('idle')
    }
  }

  // The "Update" button opens the review sheet instead of applying immediately,
  // so the owner sees the incoming changes first. Apply happens from inside the
  // sheet (which then advances the row to "Restart to finish").
  function openUpdateReview() {
    if (platformPhase !== 'idle' || !platform?.available) return
    setPlatformError('')
    setReviewOpen(true)
  }

  function closeUpdateReview() {
    setReviewOpen(false)
    // useDialogFocus first restores the opener captured at mount. A successful
    // apply can replace that node during refreshPlatform, so focus the live
    // conditional action after React commits the close and replacement.
    requestAnimationFrame(() => {
      platformActionRef.current?.focus({ preventScroll: true })
    })
  }

  // Poll until the restart is actually safe to navigate. A plain successful
  // /api/health response can still be the OLD worker answering before its
  // BackgroundTask sends SIGTERM, so prefer a changed boot id. When updating
  // from an older server that does not expose boot ids, require either a
  // down/up cycle or a conservative wait before trying to reload.
  function pollRestartThenReload({ previousBootId = '', onTimeout }) {
    clearRestartPoll()
    const startedAt = Date.now()
    let attempts = 0
    let sawUnavailable = false

    const poll = async () => {
      restartPollRef.current = null
      attempts += 1
      try {
        const health = await readRestartHealth()
        if (!health.ok) {
          sawUnavailable = true
        } else if (restartCanReload({
          previousBootId,
          currentBootId: health.bootId,
          sawUnavailable,
          elapsedMs: Date.now() - startedAt,
        })) {
          const reloaded = await reloadOntoFreshSW()
          if (reloaded) return
        }
      } catch {
        sawUnavailable = true
      }
      if (attempts >= RESTART_POLL_MAX) {
        onTimeout()
        return
      }
      restartPollRef.current = window.setTimeout(poll, RESTART_POLL_INTERVAL_MS)
    }

    restartPollRef.current = window.setTimeout(poll, RESTART_POLL_INTERVAL_MS)
  }

  // The owner's explicit confirmation that finishes a platform update: clicking
  // this IS the confirm — nothing restarts on its own.
  async function restartToFinish() {
    if (platformPhase === 'restarting') return
    setPlatformError('')
    setPlatformPhase('restarting')
    try {
      const previousBootId = await readRestartBootId()
      // apiFetch resolves for any non-401, so a 5xx is NOT a thrown error —
      // check res.ok or we'd poll + reload onto the same (unchanged) code and
      // report a non-restart as success. Mirrors applyPlatformUpdate's guard.
      const res = await api.platform.restart()
      if (!res.ok) {
        let detail = ''
        try { detail = (await res.json())?.detail || '' } catch {}
        setPlatformError(detail ? `Restart failed: ${detail}` : 'Restart signal failed.')
        setPlatformPhase('idle')
        return
      }
      returnToSettingsAfterReload()
      pollRestartThenReload({
        previousBootId,
        onTimeout: () => {
          setPlatformError("Server hasn't come back yet — check the container.")
          setPlatformPhase('idle')
        },
      })
    } catch {
      setPlatformError('Restart signal failed.')
      setPlatformPhase('idle')
    }
  }

  const version = versionQuery.data
  // Show the upstream commit the local platform is reconciled to as the
  // user-facing version. A reconcile/rebase can create a local served commit
  // whose SHA does not exist on GitHub even though it fully contains
  // origin/main; keep that identity as a secondary diagnostic instead of
  // presenting it as the published Möbius version.
  const mobiusVersion = platformVersionIdentity(platform, version)
  // The commit date (YYYY-MM-DD) baked at build time, shown next to the sha as
  // a human "version · date". Null on a dev build that didn't stamp it.
  const buildDate = version?.build_date && version.build_date !== 'unknown'
    ? version.build_date
    : null
  // Derived state for the single "Möbius" update row (see the section below).
  const platformConflict = platform?.state === 'conflict'
  // A text-clean update that failed the post-rebase import probe was rolled back
  // to the previous served version — the update is still available, but its last
  // apply needs a repair pass, so the row says so distinctly rather than reading
  // as a plain "New update available".
  const platformRolledBack = platform?.state === 'rolled_back'
  const platformRestart = !!platform?.needs_restart
  const updateAvailable = !!platform?.available
  const mobiusUpdating =
    platformPhase === 'applying' || updatePhase === 'checking'
  const checkUpdatesLabel = updateCheckLabel(updatePhase)
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
                  version={codexVersion}
                  expanded={expandedAuth === 'codex'}
                  onToggleExpand={toggleCodexAuth}
                >
                  <CodexAuth onConnected={onCodexAuthDone} />
                </ProviderRow>

                <ProviderRow
                  name="Claude Code"
                  connected={claudeAuthenticated}
                  version={claudeVersion}
                  expanded={expandedAuth === 'claude'}
                  onToggleExpand={toggleClaudeAuth}
                >
                  <ProviderAuth
                    authenticated={claudeAuthenticated}
                    compact
                    onDone={onClaudeAuthDone}
                  />
                </ProviderRow>

                <ProviderRow
                  name="Chat model"
                  connected
                  subtitle="Choose which models appear. New chats use your last pick."
                  statusNode={
                    <span className="provider-row__status-text settings__last-model">
                      {lastModelLabel ? (
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
                className={`settings-agent-group${attentionSection === 'background-agents' ? ' settings-setup-target' : ''}`}
                id="settings-background-agents"
                ref={(node) => setSetupFocusRef('background-agents', node)}
                tabIndex={-1}
              >
                <div className="settings-agent-group__head">
                  <div className="settings-agent-group__title-row">
                    <h3 className="settings__agent-title">Background agents</h3>
                  </div>
                  <p className="settings__subtext settings__subtext--tight">
                    Used for memory, reflection, and other automatic tasks. Tried in order.
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
                    color="danger"
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
                <Sun size={17} strokeWidth={1.8} />
              </span>
              <span
                className={`settings__appearance-option${themeMode === 'dark' ? ' settings__appearance-option--active' : ''}`}
                aria-hidden="true"
              >
                <Moon size={17} strokeWidth={1.8} />
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

        {/* ONE honest "Möbius" update surface. Folds platform availability,
            conflict repair, and restart-to-finish into a single row: one status
            line, one action. Priority: an in-progress conflict, then a pending
            restart, then an available update, then up to date. Per-layer
            mechanics stay invisible (the SW + platform_update.py). Always
            renders (it carries the Möbius build identity), so there is no second
            surface. The action slot holds exactly one contextual button; when
            up to date that button is an explicit "Check for updates" (git fetch
            + SW re-check) with its own "Checking…" text — it never mutates the
            status label beside it. */}
        <section className="settings__section settings__section--compact">
          <h2 className="settings__section-title">Möbius</h2>
          <div className="settings__row settings__row--top">
            <div className="settings__update">
              <StatusDot
                color={platformConflict || platformRolledBack || platformRestart || updateAvailable ? '--accent' : '--green'}
              >
                {platformConflict
                  ? 'Update blocked'
                  : platformRolledBack
                    ? 'Update needs repair'
                    : platformRestart
                      ? 'Restart to finish'
                      : updateAvailable
                        ? 'New update available'
                        : 'Up to date'}
              </StatusDot>
              {mobiusVersion.primarySha && (
                <p className="settings__build">
                  {mobiusVersion.synced ? 'Synced to ' : 'Serving '}
                  <span className="settings__standard-highlight">{mobiusVersion.primarySha}</span>
                  {!mobiusVersion.synced && buildDate ? ` · ${buildDate}` : ''}
                </p>
              )}
            </div>
            {platformConflict ? (
              onOpenChat ? (
                <button
                  ref={platformActionRef}
                  className="settings__btn settings__btn--outline settings__btn--sm settings__btn--nowrap"
                  type="button"
                  onClick={resolvePlatformConflict}
                  disabled={platformPhase === 'resolving'}
                >
                  {platformPhase === 'resolving'
                    ? 'Opening…'
                    : platform?.conflict_chat_id
                      ? 'Open chat'
                      : 'Resolve in chat'}
                </button>
              ) : null
            ) : platformRestart ? (
              <button
                ref={platformActionRef}
                className="settings__btn settings__btn--sm settings__btn--nowrap"
                type="button"
                onClick={restartToFinish}
                disabled={platformPhase === 'restarting'}
              >
                {/* "Restart to finish", not bare "Restart" — the Server section's
                    standalone "Restart" sits in the same view, so the word
                    "Restart" must mean exactly one thing. This one completes a
                    pending update; the label says so. */}
                {platformPhase === 'restarting' ? 'Restarting…' : 'Restart to finish'}
              </button>
            ) : updateAvailable ? (
              <button
                ref={platformActionRef}
                className="settings__btn settings__btn--sm settings__btn--nowrap"
                type="button"
                onClick={openUpdateReview}
                disabled={mobiusUpdating}
              >
                {mobiusUpdating ? 'Updating…' : 'Review update'}
              </button>
            ) : (
              // Nothing to apply — offer an explicit refresh. Subtle outline (a
              // secondary action, not a call-to-action) and its own transient
              // feedback text, so it never mutates the status label beside it.
              // If the check surfaces an update, this slot re-renders to the
              // Update button on the next paint.
              <button
                ref={platformActionRef}
                className="settings__btn settings__btn--outline settings__btn--sm settings__btn--nowrap"
                type="button"
                onClick={checkForUpdates}
                disabled={updatePhase === 'checking'}
              >
                {checkUpdatesLabel}
              </button>
            )}
          </div>
          {platformPhase === 'restarting' && (
            <div className="settings__notice" role="status">
              Restart signal sent. The page will reload shortly.
            </div>
          )}
          {platformError && (
            <Alert color="danger" variant="soft" description={platformError} />
          )}
        </section>

        {reviewOpen && (
          <UpdateReviewModal
            onClose={closeUpdateReview}
            onApply={applyPlatformUpdate}
            onResolve={resolvePlatformConflict}
            applying={platformPhase === 'applying'}
            resolving={platformPhase === 'resolving'}
            applyError={platformError}
          />
        )}

        <section className="settings__section settings__section--compact settings__section--server">
          <h2 className="settings__section-title">Server</h2>
          <div className="settings__row">
            <span className="settings__label">Restart</span>
            {restartConfirm ? (
              <div className="settings__confirm">
                <button
                  className="settings__btn settings__btn--outline settings__btn--sm"
                  type="button"
                  onClick={() => setRestartConfirm(false)}
                  disabled={restartPhase === 'restarting'}
                >
                  Cancel
                </button>
                <button
                  className="settings__btn settings__btn--sm settings__btn--nowrap"
                  type="button"
                  onClick={restartServer}
                  disabled={restartPhase === 'restarting'}
                >
                  {restartPhase === 'restarting' ? 'Restarting…' : 'Restart now'}
                </button>
              </div>
            ) : (
              <button
                className="settings__btn settings__btn--outline settings__btn--sm"
                type="button"
                onClick={() => { setRestartError(''); setRestartConfirm(true) }}
              >
                Restart
              </button>
            )}
          </div>
          {restartConfirm && restartPhase !== 'restarting' && (
            <p className="settings__subtext settings__subtext--tight">
              Restarting interrupts any chat that's mid-response.
            </p>
          )}
          {restartPhase === 'restarting' && (
            <div className="settings__notice" role="status">
              Restart signal sent. The page will reload shortly.
            </div>
          )}
          {restartError && (
            <Alert
              color="danger"
              variant="soft"
              description={restartError}
            />
          )}
          <div className="settings__row settings__row--recovery">
            <span className="settings__label">Recovery</span>
            <a className="settings__btn settings__btn--outline settings__btn--sm" href="/recover" target="_blank" rel="noopener noreferrer">
              Open
            </a>
          </div>
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
