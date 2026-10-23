import { useId, useRef } from 'react'
import useDialogFocus from '../../hooks/useDialogFocus.js'

// The owner's decision point for `device.folders` (see lib/localFolders.js).
// It is shell UI on purpose: its button click is the user activation the
// browser's folder picker and permission prompt require, and an app can never
// draw or press it.
export default function FolderAccessDialog({ request, accentStyle, onAnswer }) {
  const dialogRef = useRef(null)
  const cancelRef = useRef(null)
  const titleId = useId()
  const cancel = () => onAnswer(null)
  useDialogFocus({ containerRef: dialogRef, initialFocusRef: cancelRef, onClose: cancel })
  const appName = request.appName || 'This app'

  if (request.kind === 'reconnect') {
    return (
      <div className="canvas-file-export" role="dialog" aria-modal="true"
        aria-labelledby={titleId} ref={dialogRef} tabIndex={-1}>
        <div className="canvas-file-export__card" style={accentStyle}>
          <div className="canvas-file-export__eyebrow">FOLDER ON THIS COMPUTER</div>
          <h2 id={titleId}>Open “{request.folderName}” again?</h2>
          <p>{appName} is connected to this folder. Your browser asks once more after it restarts.</p>
          <div className="canvas-file-export__actions">
            <button type="button" ref={cancelRef} onClick={cancel}>Not now</button>
            <button type="button" className="canvas-file-export__primary"
              onClick={() => onAnswer({ allow: true })}>Allow</button>
          </div>
        </div>
      </div>
    )
  }

  const others = (request.folders || []).filter((f) => !f.connected)
  return (
    <div className="canvas-file-export" role="dialog" aria-modal="true"
      aria-labelledby={titleId} ref={dialogRef} tabIndex={-1}>
      <div className="canvas-file-export__card" style={accentStyle}>
        <div className="canvas-file-export__eyebrow">FOLDER ON THIS COMPUTER</div>
        <h2 id={titleId}>Let {appName} use a folder?</h2>
        <p>
          It can read the files in the folder you choose, and nothing else on this
          computer. Files stay where they are unless the app saves a copy.
        </p>
        {others.length > 0 && (
          <div className="canvas-folder-access__list" role="group" aria-label="Folders you already connected">
            {others.map((folder) => (
              <button key={folder.id} type="button" className="canvas-folder-access__folder"
                onClick={() => onAnswer({ folderId: folder.id })}>
                Use “{folder.name}”
              </button>
            ))}
          </div>
        )}
        <div className="canvas-file-export__actions">
          <button type="button" ref={cancelRef} onClick={cancel}>Cancel</button>
          <button type="button" className="canvas-file-export__primary"
            onClick={() => onAnswer({ pick: true })}>Choose folder…</button>
        </div>
      </div>
    </div>
  )
}
