/* A yellow warning icon beside Pick a folder; its small box says why this browser shares read-only. */
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { Warning, X } from '@openai/apps-sdk-ui/components/Icon'
import useContextMenuOutsideDismiss from '../../hooks/useContextMenuOutsideDismiss.js'
import { browserName } from '../../lib/sharedFolders/folderStore.js'

function useCloseOnEscape(open, close) {
  useEffect(() => {
    if (!open) return undefined
    const closeOnEscape = (event) => {
      if (event.key === 'Escape') close()
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [open, close])
}

// Placed in the page flow, not a floating layer: the desktop shell scales its
// page, and floating-layer positioning does not account for that scale.
export default function ReadOnlyBrowserNote() {
  const [open, setOpen] = useState(false)
  const rootRef = useRef(null)
  const noteId = useId()
  const browser = browserName()
  const close = useCallback(() => setOpen(false), [])
  useContextMenuOutsideDismiss({ open, menuRef: rootRef, onDismiss: close })
  useCloseOnEscape(open, close)

  return (
    <span className="shared-folders__note-anchor" ref={rootRef}>
      <button
        type="button"
        className="shared-folders__note-trigger"
        aria-label={`Why ${browser} shares folders read-only`}
        aria-expanded={open}
        aria-controls={noteId}
        onClick={() => setOpen(value => !value)}
      >
        <Warning width={18} height={18} aria-hidden="true" />
      </button>
      {open && (
        <div id={noteId} className="shared-folders__note" role="dialog" aria-label={`Read only in ${browser}`}>
          <div className="shared-folders__note-head">
            <span className="shared-folders__note-title">Read only in {browser}</span>
            <button type="button" className="shared-folders__note-close" aria-label="Close" onClick={close}>
              <X width={16} height={16} aria-hidden="true" />
            </button>
          </div>
          <p className="shared-folders__note-text">
            {browser} shares a folder read-only, until you leave Möbius. Chrome and Edge on a computer remember folders and allow editing.
          </p>
        </div>
      )}
    </span>
  )
}
