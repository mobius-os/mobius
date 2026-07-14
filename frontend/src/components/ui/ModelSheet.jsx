import { useRef } from 'react'
import { createPortal } from 'react-dom'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import EffortStepper from './EffortStepper.jsx'
import { modelEfforts } from './modelEfforts.js'
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
 *
 * When `efforts` is passed, a reasoning-effort stepper renders inline
 * under the selected model row (the effort scale is provider-specific,
 * so it belongs with the model choice — this mirrors the chat
 * composer's picker). Providing `efforts` also keeps the sheet OPEN on
 * a model pick so the owner can set effort in the same interaction;
 * without it, picking a model closes the sheet as before.
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
  efforts,
  effort,
  onEffortChange,
}) {
  const supportsEffort = (
    Array.isArray(efforts)
    && efforts.length > 0
    && typeof onEffortChange === 'function'
  )
  const closeRef = useRef(null)
  const dialogRef = useRef(null)
  useDialogFocus({
    open,
    containerRef: dialogRef,
    initialFocusRef: closeRef,
    onClose,
  })

  if (!open) return null

  const noneSelected = !provider
  return createPortal(
    <div
      className="model-sheet__backdrop"
      role="presentation"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={dialogRef}
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
                  const rowEfforts = supportsEffort ? modelEfforts(efforts, m) : []
                  return (
                    <div key={`${group.key}-${m.id}`}>
                      <button
                        type="button"
                        className={`model-sheet__row${on ? ' model-sheet__row--sel' : ''}`}
                        disabled={disabled}
                        onClick={() => {
                          onPick(group.key, m.id, m)
                          if (rowEfforts.length === 0) onClose()
                        }}
                      >
                        <span className="model-sheet__row-icon">{Logo && <Logo />}</span>
                        <span className="model-sheet__row-main">
                          <span className="model-sheet__row-title">{m.name || m.label || m.id}</span>
                          <span className="model-sheet__row-id">{m.id}</span>
                        </span>
                        {on && <span className="model-sheet__check" aria-hidden="true" />}
                      </button>
                      {on && rowEfforts.length > 0 && (
                        <div className="model-sheet__effort">
                          <EffortStepper
                            efforts={rowEfforts}
                            value={effort}
                            onChange={onEffortChange}
                            ariaLabel="Reasoning effort"
                          />
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )
          })}
        </div>
      </div>
    </div>,
    document.body,
  )
}
