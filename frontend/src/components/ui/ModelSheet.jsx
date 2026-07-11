import { useEffect, useRef } from 'react'
import './ModelSheet.css'

/**
 * Bottom-sheet model picker shared by Settings (background agents) and
 * the Setup wizard. Replaces the native `<select>` those surfaces used
 * to render, so the model list looks and orders identically on every
 * device instead of deferring to the OS picker wheel.
 *
 * Dumb by design: the caller resolves `groups` (each
 * `{ key, label, Logo, models: [{ id, name }] }`, already ordered) so
 * this component owns only the overlay, grouping chrome, selection
 * state, and dismissal. Ordering + hidden-model filtering live with
 * the caller, which already knows the registry.
 *
 * A disconnected provider's rows render disabled UNLESS one of them is
 * the current selection — the owner must always be able to see and
 * switch away from what's active. `allowNone` adds a leading "none"
 * row for the optional fallback slot; picking it calls `onNone`.
 */
export default function ModelSheet({
  open,
  onClose,
  title = 'Model',
  groups,
  provider,
  model,
  connectedProviders,
  onPick,
  allowNone = false,
  noneLabel = 'No fallback',
  onNone,
}) {
  const closeRef = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    // Land focus inside the dialog so Escape works and keyboard users
    // aren't stranded on the trigger behind the backdrop.
    closeRef.current?.focus?.()
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const noneSelected = !provider
  return (
    <div className="model-sheet__backdrop" role="presentation" onClick={onClose}>
      <div
        className="model-sheet"
        role="dialog"
        aria-modal="true"
        aria-label={`Choose ${title.toLowerCase()}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="model-sheet__head">
          <span className="model-sheet__title">{title}</span>
          <button
            ref={closeRef}
            type="button"
            className="model-sheet__close"
            onClick={onClose}
          >
            Close
          </button>
        </div>
        <div className="model-sheet__body">
          {allowNone && (
            <button
              type="button"
              className={`model-sheet__row${noneSelected ? ' model-sheet__row--sel' : ''}`}
              onClick={() => { onNone?.(); onClose() }}
            >
              <span className="model-sheet__row-icon" aria-hidden="true">—</span>
              <span className="model-sheet__row-main">
                <span className="model-sheet__row-title">{noneLabel}</span>
              </span>
              {noneSelected && <span className="model-sheet__check" aria-hidden="true" />}
            </button>
          )}
          {(!groups || groups.length === 0) && (
            <div className="model-sheet__empty">No models available.</div>
          )}
          {groups && groups.map((group) => {
            const connected = !connectedProviders || connectedProviders.has(group.key)
            const Logo = group.Logo
            return (
              <div key={group.key} className="model-sheet__group">
                <div className="model-sheet__group-head">
                  {Logo && <span className="model-sheet__group-icon"><Logo /></span>}
                  <span>{group.label}</span>
                  {!connected && <span className="model-sheet__group-hint">not connected</span>}
                </div>
                {group.models.map((m) => {
                  const on = provider === group.key && model === m.id
                  const disabled = !connected && !on
                  return (
                    <button
                      key={`${group.key}-${m.id}`}
                      type="button"
                      className={`model-sheet__row${on ? ' model-sheet__row--sel' : ''}`}
                      disabled={disabled}
                      onClick={() => { onPick(group.key, m.id); onClose() }}
                    >
                      <span className="model-sheet__row-icon">{Logo && <Logo />}</span>
                      <span className="model-sheet__row-main">
                        <span className="model-sheet__row-title">{m.name || m.label || m.id}</span>
                        <span className="model-sheet__row-id">{m.id}</span>
                      </span>
                      {on && <span className="model-sheet__check" aria-hidden="true" />}
                    </button>
                  )
                })}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
