/**
 * RETIRED — replaced by ComposerPopover + ChatSettingsPanel
 * (see CLAUDE.md "Composer popover — model + effort + provider, all
 * in one place"). No file in frontend/src/ imports this component
 * anymore; it's kept in the tree as a reference implementation in
 * case the slash-key entry point ever returns. Don't add new
 * importers — point them at ChatSettingsPanel instead.
 *
 * Original behavior, preserved for context: "/" button next to
 * file-attach in the chat composer, opens a popover with provider /
 * model / effort pickers scoped to THIS chat (per-chat override),
 * persistence via PATCH /api/chats/{id} with `agent_settings_json`,
 * NEXT-TURN-ONLY apply semantics, provider-locked once the chat has
 * assistant turns. Codex model swaps emit a one-time "model-switch"
 * system message on the next turn.
 */

import { useEffect, useRef, useState, useCallback } from 'react'
import { apiFetch } from '../../api/client.js'
import ProviderModelPicker, {
  modelsForProvider,
} from '../ProviderModelPicker/ProviderModelPicker.jsx'
import './SlashPicker.css'


/** Tiny `/` icon. Stroke-current so the button picks up theme color. */
function SlashIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round"
    >
      <path d="M11.5 2.5l-7 11" />
    </svg>
  )
}


/**
 * Props:
 *   chatId         — required; PATCH target
 *   provider       — the chat's current provider ('claude' or 'codex')
 *   effective      — {model, effort, codex_model} the next turn will use
 *   hasAssistantTurns — when true, picking the OTHER provider's model
 *                       is disabled (sessions aren't cross-portable)
 *   onChange(next)  — fires after a successful PATCH with the new
 *                     effective settings, so the parent can update
 *                     its cached chat row without refetching
 */
export default function SlashPicker({
  chatId,
  provider,
  effective,
  hasAssistantTurns,
  onChange,
}) {
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  // Set of provider ids the user has authenticated. Drives the
  // section-filter in ProviderModelPicker so disconnected providers'
  // models don't clutter the list. Null until first fetch resolves.
  const [connectedProviders, setConnectedProviders] = useState(null)
  const wrapRef = useRef(null)
  // Monotonic id so an earlier, slower PATCH response can't "snap"
  // the UI back to a value the user has already changed away from.
  // Rapid model/effort flips fire overlapping requests; we only
  // accept the response whose reqId matches the latest dispatched.
  const latestReqId = useRef(0)

  // Live editing state seeded from the effective settings. Edits
  // are optimistic (UI updates immediately) but a PATCH failure
  // rolls back. The backend guarantees `effective` always has a
  // valid model + effort for this provider (see providers.py
  // effective_agent_settings fallbacks).
  const [draftModel, setDraftModel] = useState(effective?.model || '')
  const [draftEffort, setDraftEffort] = useState(effective?.effort || '')
  // The provider radio is driven by the chat's current provider; a
  // PATCH that switches providers updates this immediately and rolls
  // back if the request fails.
  const [draftProvider, setDraftProvider] = useState(provider || 'claude')

  // Re-seed when the chat row changes underneath us (e.g. another
  // device patched, or the user opened a different chat).
  useEffect(() => {
    setDraftModel(effective?.model || '')
    setDraftEffort(effective?.effort || '')
  }, [effective?.model, effective?.effort, chatId])

  useEffect(() => {
    setDraftProvider(provider || 'claude')
  }, [provider, chatId])

  // Fetch which providers are authenticated when the popover opens.
  // Doing this on-open (rather than on mount) keeps the badge in the
  // composer cheap — the cost only lands when the user actually opens
  // the picker. A second open re-fetches so a fresh /connect from
  // Settings is reflected without a page reload.
  useEffect(() => {
    if (!open) return
    let cancelled = false
    apiFetch('/auth/providers/status')
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (cancelled || !data) return
        const ids = Object.entries(data)
          .filter(([, v]) => v?.authenticated)
          .map(([k]) => k)
        setConnectedProviders(ids)
      })
      .catch(() => { /* silent — picker falls back to "show all" */ })
    return () => { cancelled = true }
  }, [open])

  // Close on outside click / Escape so the popover behaves like the
  // attach-file picker and other transient drop-ups in the composer.
  useEffect(() => {
    if (!open) return
    function onPointer(e) {
      if (!wrapRef.current) return
      if (wrapRef.current.contains(e.target)) return
      setOpen(false)
    }
    function onKey(e) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  // Returns one of three outcomes so callers can distinguish:
  //   'ok'    — PATCH succeeded and applied
  //   'stale' — a newer PATCH has already been dispatched; the caller
  //             should NOT roll back optimistic state because the
  //             newer PATCH is now the authoritative one
  //   'fail'  — real failure (HTTP non-2xx or network error); the
  //             caller should roll back
  // Stale was previously collapsed into `fail`, which made rapid
  // picks clobber each other: the older PATCH's failure rollback
  // would overwrite the newer pick's optimistic state.
  const patchChat = useCallback(async (body) => {
    if (!chatId) return 'fail'
    const reqId = ++latestReqId.current
    setSaving(true)
    setError('')
    try {
      const res = await apiFetch(`/chats/${chatId}`, {
        method: 'PATCH',
        body: JSON.stringify(body),
      })
      if (reqId !== latestReqId.current) return 'stale'
      if (!res.ok) {
        setError('Could not save. Try again.')
        return 'fail'
      }
      const data = await res.json()
      onChange?.({
        agent_settings_json: data.agent_settings_json,
        provider: data.provider,
        effective: data.effective,
      })
      return 'ok'
    } catch {
      if (reqId !== latestReqId.current) return 'stale'
      setError('Network error.')
      return 'fail'
    } finally {
      if (reqId === latestReqId.current) setSaving(false)
    }
  }, [chatId, onChange])

  const handleEffortChange = useCallback((value) => {
    setDraftEffort(value)
    patchChat({ agent_settings_json: { effort: value } })
  }, [patchChat])

  // Single handler for the combined-list picker: picking a row tells
  // us BOTH the model and which provider it belongs to. When the
  // provider differs from the current chat's provider we send both in
  // one PATCH so the backend writes them atomically (avoids the
  // provider-then-model two-write race that briefly left the chat
  // with a stale-provider model). Rollback fires ONLY on a real
  // failure — a 'stale' outcome means a newer PATCH has won, and
  // rolling back would clobber that newer state.
  const handlePickModel = useCallback(async (value, providerValue) => {
    const prevProvider = draftProvider
    const prevModel = draftModel
    setDraftModel(value)
    if (providerValue !== draftProvider) {
      setDraftProvider(providerValue)
      const outcome = await patchChat({
        provider: providerValue,
        agent_settings_json: { model: value },
      })
      if (outcome === 'fail') {
        setDraftProvider(prevProvider)
        setDraftModel(prevModel)
      }
      return
    }
    const outcome = await patchChat({
      agent_settings_json: { model: value },
    })
    if (outcome === 'fail') setDraftModel(prevModel)
  }, [draftProvider, draftModel, patchChat])

  // Tooltip text for the slash button. Surfaces what the next turn
  // will use without consuming composer space — the user gets the
  // information on hover (desktop) or long-press (mobile) but the
  // button stays icon-only. See Change 3 in the May 2026 UX pass.
  const models = modelsForProvider(draftProvider)
  const activeModelEntry = models.find(m => m.value === draftModel)
  const providerLabel = draftProvider === 'codex' ? 'Codex' : 'Claude'
  const tooltip = activeModelEntry?.label
    ? `${providerLabel} — ${activeModelEntry.label}`
    : providerLabel

  const isCodex = draftProvider === 'codex'
  const codexSwitchWarning = (
    isCodex && hasAssistantTurns
    && effective?.model && draftModel
    && draftModel !== effective?.model
  )
  // Used to be: `hasAssistantTurns ? provider : null` (hard-disable
  // the other provider's rows once the chat had an assistant turn,
  // on the theory that SDK sessions aren't cross-portable). But
  // backend `patch_chat` already wipes `session_id` on provider
  // change, and the new SDK session reads the prior chat messages
  // as context — the user keeps their transcript, only the
  // provider's internal session caches reset. Hard-disabling led
  // to silent failures where the user tapped the other provider,
  // the radio's `onChange` didn't fire, and the chat stayed on the
  // original provider. Allow the switch; let the user own the
  // decision.
  const lockedToProvider = null

  return (
    <div className="slash" ref={wrapRef}>
      <button
        type="button"
        className={`slash__btn${open ? ' slash__btn--open' : ''}`}
        onClick={() => setOpen(o => !o)}
        aria-label={`Chat settings (${tooltip})`}
        aria-expanded={open}
        title={tooltip}
      >
        <SlashIcon />
      </button>
      {open && (
        <div className="slash__popover" role="dialog">
          <ProviderModelPicker
            provider={draftProvider}
            model={draftModel}
            effort={draftEffort}
            onPickModel={handlePickModel}
            onChangeEffort={handleEffortChange}
            lockedToProvider={lockedToProvider}
            connectedProviders={connectedProviders}
          />
          <div className="slash__foot">
            {codexSwitchWarning && (
              <p className="slash__note">
                Codex injects a one-time model-switch note on the next
                reply — may briefly affect that turn.
              </p>
            )}
            {error && <p className="slash__error">{error}</p>}
          </div>
        </div>
      )}
    </div>
  )
}
