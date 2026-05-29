/**
 * ManageModelsModal — owner-level model-picker preferences editor.
 *
 * Opened from the "+ Manage models" affordance at the bottom of
 * ChatSettingsPanel. Lists every model in the live registry,
 * grouped by provider, with a per-row "show in picker" toggle.
 *
 * Persistence shape:
 *   PATCH /api/owner/model-prefs
 *   body: { hidden_ids: ["claude-haiku-4-5-20251001", ...] }
 *
 * The picker filter is the SOURCE OF TRUTH for which models show up
 * in ChatSettingsPanel; this modal is the only place to edit it.
 * The currently-selected model in a chat is always visible in the
 * picker even when hidden here (the picker enforces that override),
 * so the owner can never accidentally lock themselves out of their
 * own active chat.
 *
 * Design language follows SettingsView — cards on the surface bg,
 * 1px borders, sentence-case section titles, accent toggles. The
 * dark-themed overlay matches the existing `.popover` backdrop
 * pattern (no separate overlay component — we paint our own).
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { modelQueries } from '../../hooks/queries.js'
import './ManageModelsModal.css'


export default function ManageModelsModal({
  onClose,
  providerOrder,
  providerInfo,
}) {
  const queryClient = useQueryClient()
  // Read straight from cache — by the time the modal opens, the
  // panel has already triggered both queries and they've resolved
  // (the panel's `dataReady` gate guarantees it). If for some reason
  // the cache is empty (e.g. modal opened via a deep link in a
  // future iteration), we still kick off both queries and render the
  // skeleton while we wait.
  const registryQuery = modelQueries.registry.useQuery()
  const prefsQuery = modelQueries.prefs.useQuery()
  const registry = registryQuery.data
  const persistedHidden = prefsQuery.data?.hidden_ids || []

  // Local draft state — the modal commits on Save, not on every
  // toggle. This lets the user audit their changes and matches the
  // existing settings form pattern (Save button at bottom).
  const [draftHidden, setDraftHidden] = useState(() => new Set(persistedHidden))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)

  // Re-seed the draft when persisted prefs change underneath us
  // (e.g. another tab edited them). Skipping this would silently
  // drop the other-tab's changes the next time the user opens this
  // modal in this tab.
  useEffect(() => {
    setDraftHidden(new Set(persistedHidden))
    // The set identity matters, not the array reference — compare
    // by content (join is cheap for small arrays; this list is
    // bounded by the registry size).
  }, [persistedHidden.join('|')])

  // True when the draft differs from what's persisted. Disables the
  // Save button so the modal feels right when the user opens it
  // without changing anything. Hoisted above the close handlers so
  // those handlers can read it without a forward dependency.
  const dirty = useMemo(() => {
    if (draftHidden.size !== persistedHidden.length) return true
    for (const id of persistedHidden) if (!draftHidden.has(id)) return true
    return false
  }, [draftHidden, persistedHidden])

  // Dismiss-with-dirty-guard. Escape, overlay-click, and the explicit
  // Cancel button all route through this so a user can't silently lose
  // toggle changes by reaching for the keyboard or tapping outside.
  // When clean, closes immediately; when dirty, confirms first.
  const tryClose = useCallback(() => {
    if (dirty && !window.confirm('Discard unsaved changes?')) return
    onClose()
  }, [dirty, onClose])

  // Escape-to-close. Mirrors ComposerPopover's escape handling so
  // dismissal behavior is consistent across modals.
  useEffect(() => {
    function onKey(e) { if (e.key === 'Escape') tryClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [tryClose])

  const toggle = useCallback((modelId) => {
    setDraftHidden(prev => {
      const next = new Set(prev)
      if (next.has(modelId)) next.delete(modelId)
      else next.add(modelId)
      return next
    })
  }, [])

  const handleSave = useCallback(async () => {
    setSaving(true)
    setError('')
    try {
      const res = await api.owner.modelPrefs.save([...draftHidden])
      if (!res.ok) {
        setError('Could not save preferences. Try again.')
        return
      }
      // Invalidate both: registry doesn't change but prefs did, and
      // the picker reads prefs to filter. Other components that
      // consume prefs (none today; future surfaces) get the update
      // through the same cache key.
      modelQueries.prefs.invalidate(queryClient)
      onClose()
    } catch {
      setError('Network error.')
    } finally {
      setSaving(false)
    }
  }, [draftHidden, queryClient, onClose])

  // Force-refresh registry — bypasses the 5-minute server cache so
  // the owner can pull a just-released model on demand. The picker
  // cache also refreshes since both consume the same query key.
  const handleRefresh = useCallback(async () => {
    setRefreshing(true)
    setError('')
    try {
      const res = await api.models.list({ refresh: true })
      if (!res.ok) {
        setError('Could not refresh models. Try again.')
        return
      }
      const data = await res.json()
      // Push fresh data into the query cache. TanStack's notify path
      // wakes any active subscriber (this modal, plus the picker if
      // it's mounted), so an explicit invalidate would just trigger a
      // redundant background refetch right after we already have the
      // current data in hand.
      queryClient.setQueryData(
        modelQueries.keys.registry,
        data?.providers || {},
      )
    } catch {
      setError('Network error.')
    } finally {
      setRefreshing(false)
    }
  }, [queryClient])

  const ready = !!registry

  return (
    <div
      className="mmm__overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="mmm-title"
      onClick={tryClose}
    >
      <div className="mmm" onClick={(e) => e.stopPropagation()}>
        <div className="mmm__head">
          <h2 id="mmm-title" className="mmm__title">Manage models</h2>
          <button
            type="button"
            className="mmm__close"
            onClick={tryClose}
            aria-label="Close"
          >×</button>
        </div>
        <p className="mmm__subtext">
          Hide models you don't want to see in the chat picker. The
          model a chat is currently using stays visible there even
          if you hide it here.
        </p>

        {!ready && (
          <div className="mmm__skeleton" aria-hidden="true">
            <div className="mmm__skeleton-row" />
            <div className="mmm__skeleton-row" />
            <div className="mmm__skeleton-row" />
          </div>
        )}

        {ready && providerOrder.map(pid => {
          const info = providerInfo[pid]
          const entries = registry[pid] || []
          if (entries.length === 0) return null
          return (
            <section key={pid} className="mmm__section">
              <div className="mmm__section-head">
                <span className="mmm__section-icon"><info.Logo /></span>
                <span className="mmm__section-title">{info.label}</span>
              </div>
              <div className="mmm__rows">
                {entries.map(m => {
                  const visible = !draftHidden.has(m.id)
                  return (
                    <label
                      key={m.id}
                      className={`mmm-row${visible ? '' : ' mmm-row--hidden'}`}
                    >
                      <span className="mmm-row__main">
                        <span className="mmm-row__title">{m.label}</span>
                        <span className="mmm-row__sub">
                          {m.id}
                          {m.available === false && (
                            <span className="mmm-row__pill">legacy</span>
                          )}
                        </span>
                      </span>
                      <input
                        type="checkbox"
                        className="mmm-row__toggle"
                        checked={visible}
                        onChange={() => toggle(m.id)}
                        aria-label={`${visible ? 'Hide' : 'Show'} ${m.label} in picker`}
                      />
                    </label>
                  )
                })}
              </div>
            </section>
          )
        })}

        {error && <p className="mmm__error">{error}</p>}

        <div className="mmm__foot">
          <button
            type="button"
            className="mmm__btn mmm__btn--outline"
            onClick={handleRefresh}
            disabled={refreshing || !ready}
          >
            {refreshing ? 'Refreshing…' : 'Refresh models'}
          </button>
          <div className="mmm__foot-spacer" />
          <button
            type="button"
            className="mmm__btn mmm__btn--ghost"
            onClick={tryClose}
          >
            Cancel
          </button>
          <button
            type="button"
            className="mmm__btn"
            onClick={handleSave}
            disabled={saving || !dirty}
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}
