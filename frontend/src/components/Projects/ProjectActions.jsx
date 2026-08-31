import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import Ellipsis from 'lucide-react/dist/esm/icons/ellipsis.mjs'
import Pencil from 'lucide-react/dist/esm/icons/pencil.mjs'
import Trash2 from 'lucide-react/dist/esm/icons/trash-2.mjs'
import './Projects.css'

const PROJECT_COLORS = [
  { name: 'Default', value: null },
  { name: 'Violet', value: '#8b5cf6' },
  { name: 'Blue', value: '#3b82f6' },
  { name: 'Cyan', value: '#0891b2' },
  { name: 'Green', value: '#16a34a' },
  { name: 'Amber', value: '#d97706' },
  { name: 'Rose', value: '#e11d48' },
]

// One project action surface shared by the Directory and Drawer. The Directory
// keeps a visible mobile doorway; Drawer rows expose the same actions through
// their native context-menu gesture without adding another trailing control.
export default function ProjectActions({
  project,
  contextTargetRef,
  disabled = false,
  showTrigger = true,
  onRename,
  onColor,
  onDelete,
}) {
  const [open, setOpen] = useState(false)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [position, setPosition] = useState({ left: 12, top: 12 })
  const rootRef = useRef(null)
  const menuRef = useRef(null)

  const close = useCallback(() => {
    if (busy) return
    setOpen(false)
    setConfirmingDelete(false)
    setError('')
  }, [busy])

  const showMenu = useCallback((anchor) => {
    if (disabled) return
    const width = 224
    const height = 250
    setPosition({
      left: Math.max(8, Math.min(anchor.x - width, window.innerWidth - width - 8)),
      top: Math.max(8, Math.min(anchor.y + 7, window.innerHeight - height - 8)),
    })
    setOpen(true)
    setConfirmingDelete(false)
    setError('')
  }, [disabled])

  useEffect(() => {
    const target = contextTargetRef?.current
    if (!target) return undefined
    function onContextMenu(event) {
      event.preventDefault()
      showMenu({ x: event.clientX + 8, y: event.clientY })
    }
    target.addEventListener('contextmenu', onContextMenu)
    return () => target.removeEventListener('contextmenu', onContextMenu)
  }, [contextTargetRef, showMenu])

  useEffect(() => {
    if (!open) return undefined
    function onPointer(event) {
      if (
        !rootRef.current?.contains(event.target)
        && !menuRef.current?.contains(event.target)
      ) close()
    }
    function onKey(event) {
      if (event.key === 'Escape') close()
    }
    document.addEventListener('pointerdown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, close])

  async function remove() {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const deleted = await onDelete?.(project)
      if (deleted === false) {
        setError('This project could not be deleted.')
        setBusy(false)
        return
      }
      setOpen(false)
    } catch (cause) {
      setError(cause?.message || 'This project could not be deleted.')
      setBusy(false)
    }
  }

  async function changeColor(color) {
    if (busy) return
    if ((project.color || null) === color) {
      close()
      return
    }
    setBusy(true)
    setError('')
    try {
      await onColor?.(project, color)
      setOpen(false)
    } catch (cause) {
      setError(cause?.message || 'Could not change this project color.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div ref={rootRef} className={`project-menu project-actions${showTrigger ? '' : ' project-actions--context-only'}`}>
      {showTrigger && (
        <button
          type="button"
          className="project-icon-button"
          aria-label={`Actions for ${project.name}`}
          title="Project actions"
          aria-haspopup="menu"
          aria-expanded={open}
          disabled={disabled}
          onClick={event => {
            if (open) close()
            else {
              const rect = event.currentTarget.getBoundingClientRect()
              showMenu({ x: rect.right, y: rect.bottom })
            }
          }}
        >
          <Ellipsis size={19} />
        </button>
      )}
      {open && createPortal(
        <>
          <button type="button" className="project-menu__scrim" aria-label="Close project actions" onClick={close} />
          <div ref={menuRef} className="project-menu__popover project-actions__popover" role="menu" style={position}>
            <div className="project-actions__heading">
              <strong>{confirmingDelete ? 'Delete project?' : project.name}</strong>
              {confirmingDelete && <small>Files and chats can be recovered for 7 days.</small>}
            </div>
            {confirmingDelete ? (
              <div className="project-actions__confirm">
                {error && <p role="alert">{error}</p>}
                <button type="button" disabled={busy} onClick={() => setConfirmingDelete(false)}>Cancel</button>
                <button type="button" className="project-menu__danger project-actions__delete" disabled={busy} onClick={() => void remove()}>
                  <Trash2 size={16} /> {busy ? 'Deleting…' : 'Delete project'}
                </button>
              </div>
            ) : (
              <>
                {onColor && (
                  <div className="project-actions__color">
                    <span>Color</span>
                    <div className="project-actions__palette" role="group" aria-label="Project color">
                      {PROJECT_COLORS.map(option => {
                        const selected = (project.color || null) === option.value
                        return (
                          <button
                            key={option.name}
                            type="button"
                            className={`project-actions__swatch${option.value ? '' : ' project-actions__swatch--default'}${selected ? ' project-actions__swatch--selected' : ''}`}
                            style={option.value ? { '--swatch-color': option.value } : undefined}
                            title={option.name}
                            aria-label={`${option.name} project color`}
                            aria-pressed={selected}
                            disabled={busy}
                            onClick={() => void changeColor(option.value)}
                          />
                        )
                      })}
                    </div>
                  </div>
                )}
                {error && <p className="project-actions__error" role="alert">{error}</p>}
                <button type="button" role="menuitem" disabled={busy} onClick={() => { close(); onRename?.(project) }}><Pencil size={16} /> Rename</button>
                <button type="button" role="menuitem" className="project-menu__danger" disabled={busy} onClick={() => setConfirmingDelete(true)}><Trash2 size={16} /> Delete project</button>
              </>
            )}
          </div>
        </>,
        document.body,
      )}
    </div>
  )
}
