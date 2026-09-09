/* KeyboardShortcutsDialog is the dedicated Cmd/Ctrl+/ reference for shell actions. */
import { createPortal } from 'react-dom'
import { useMemo, useRef } from 'react'
import { X } from '@openai/apps-sdk-ui/components/Icon'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { shortcutReferenceGroups } from '../../lib/keyboardShortcuts.js'
import './KeyboardShortcutsDialog.css'

export default function KeyboardShortcutsDialog({ commands = [], onClose }) {
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  const groups = useMemo(() => shortcutReferenceGroups(commands), [commands])

  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: closeRef,
    onClose,
  })

  return createPortal(
    <div
      className="keyboard-shortcuts__overlay"
      role="presentation"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={dialogRef}
        className="keyboard-shortcuts"
        role="dialog"
        aria-modal="true"
        aria-labelledby="keyboard-shortcuts-title"
      >
        <header className="keyboard-shortcuts__header">
          <div>
            <h2 id="keyboard-shortcuts-title">Keyboard shortcuts</h2>
            <p>Move around Möbius without leaving the keyboard.</p>
          </div>
          <button
            ref={closeRef}
            type="button"
            className="keyboard-shortcuts__close"
            aria-label="Close keyboard shortcuts"
            onClick={onClose}
          >
            <X width={20} height={20} aria-hidden="true" />
          </button>
        </header>

        <div className="keyboard-shortcuts__groups">
          {groups.map(group => (
            <section key={group.category} className="keyboard-shortcuts__group">
              <h3>{group.category}</h3>
              <div className="keyboard-shortcuts__list">
                {group.items.map(command => (
                  <div
                    key={command.id}
                    className={`keyboard-shortcuts__row${command.enabled === false ? ' keyboard-shortcuts__row--unavailable' : ''}`}
                  >
                    <span className="keyboard-shortcuts__copy">
                      <strong>{command.title}</strong>
                      <span>{command.enabled === false && command.unavailableReason
                        ? command.unavailableReason
                        : command.description}</span>
                    </span>
                    <span
                      className="keyboard-shortcuts__bindings"
                      aria-label={command.shortcutLabels.join(' or ')}
                    >
                      {command.shortcutLabels.map((label, index) => (
                        <kbd key={`${label}-${index}`}>{label}</kbd>
                      ))}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>
    </div>,
    document.body,
  )
}
