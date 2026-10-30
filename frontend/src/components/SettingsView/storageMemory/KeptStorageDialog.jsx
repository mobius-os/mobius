/* Pop-up listing what Möbius keeps to run, with plain words for each folder. */
import { useRef } from 'react'
import { X } from '@openai/apps-sdk-ui/components/Icon'
import useDialogFocus from '../../../hooks/useDialogFocus.js'
import { formatBytes, keptDescription, keptTotal, storedSize } from './storageMemoryPresentation.js'
// The Settings pop-up look is owned by the update review; reuse it so both
// Settings dialogs open, size and close the same way.
import '../UpdateReviewModal.css'

// Contribute owns these copies and removes finished ones itself, so the box
// sends the owner there instead of deleting work that may be unsent.
const CONTRIBUTE_OWNED = new Set(['contributions', 'worktrees'])

export default function KeptStorageDialog({ kept, onClose, onOpenApp, restoreFocusRef, inertBoundaryRef }) {
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  useDialogFocus({
    containerRef: dialogRef, initialFocusRef: closeRef, restoreFocusRef, onClose,
    modal: false, lockScroll: false, inertBoundaryRef,
  })
  const openContribute = () => { onClose(); onOpenApp('contribute') }

  return (
    <div className="urm__overlay" role="presentation" onClick={onClose}>
      <div ref={dialogRef} className="urm storage-memory-kept" role="dialog" aria-modal="false"
        aria-labelledby="storage-memory-kept-title" onClick={event => event.stopPropagation()}>
        <div className="urm__head">
          <h2 id="storage-memory-kept-title" className="urm__title">Kept to run Möbius</h2>
          <button ref={closeRef} type="button" className="urm__close" onClick={onClose} aria-label="Close">
            <X width={20} height={20} aria-hidden="true" />
          </button>
        </div>
        <div className="urm__body">
          <p className="storage-memory-kept__intro">
            These folders hold your chats, apps, app data and Möbius itself. Clear never deletes them,
            also not when storage is low. Together they use {formatBytes(keptTotal(kept))}.
          </p>
          <ul className="storage-memory-kept__list">
            {kept.map(row => (
              <li key={row.id} className="storage-memory-kept__row">
                <div className="storage-memory-kept__text">
                  <span className="storage-memory-kept__name">{row.label}</span>
                  {keptDescription(row.id) && <span className="storage-memory-kept__sub">{keptDescription(row.id)}</span>}
                </div>
                <span className="storage-memory__size">{storedSize(row).value}</span>
              </li>
            ))}
          </ul>
          {onOpenApp && kept.some(row => CONTRIBUTE_OWNED.has(row.id)) && (
            <div className="storage-memory-kept__contribute">
              <p>Work and contribution copies can hold changes that are not sent yet. Contribute shows them and removes finished ones.</p>
              <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={openContribute}>Open Contribute</button>
            </div>
          )}
          <p className="storage-memory-kept__note">For more room, clear agent files in Storage and Memory, or grow the storage in your hosting plan.</p>
        </div>
      </div>
    </div>
  )
}
