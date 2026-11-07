/* A decision box above the chat box: share the folder of a big upload instead of storing copies. */
import { useRef, useState } from 'react'
import { X } from '@openai/apps-sdk-ui/components/Icon'
import useSharedFolders from '../../hooks/useSharedFolders.js'
import { copyByteLabel } from '../../lib/projectCopies.js'
import { addVisitFolder, shareUploadFolder } from '../../lib/sharedFolders/folderActions.js'
import './QuestionCard.css'
import './SharedFolderUploadPrompt.css'

/** Share the upload's folder read-only: remembered in Chrome and Edge, for this visit elsewhere. */
function useShareFolder(canRemember, onShared) {
  const [error, setError] = useState(null)
  const visitInput = useRef(null)
  const finish = (outcome) => {
    if (outcome?.cancelled) return
    if (!outcome?.ok) {
      setError(outcome?.message || 'The folder could not be shared. Try again.')
      return
    }
    setError(null)
    onShared(outcome.name)
  }
  // The folder picker needs this tap's activation, so it opens before any await.
  const share = async () => {
    if (!canRemember) {
      visitInput.current?.click()
      return
    }
    finish(await shareUploadFolder())
  }
  const onVisitFiles = async (event) => {
    finish(await addVisitFolder(event.target.files))
    event.target.value = ''
  }
  return { error, visitInput, share, onVisitFiles }
}

function UploadChoices({ canRemember, size, onShare, onUploadAnyway }) {
  return (
    <div className="qcard__opts">
      <button type="button" className="qcard__opt upload-folder-prompt__share" onClick={onShare}>
        <span className="qcard__opt-body">
          <span className="qcard__opt-label">Share the folder</span>
          <span className="qcard__opt-desc">
            {canRemember
              ? 'Pick the folder that holds them. Read only.'
              : 'Pick the folder that holds them. Read only, for this visit.'}
          </span>
        </span>
      </button>
      <button type="button" className="qcard__opt" onClick={onUploadAnyway}>
        <span className="qcard__opt-body">
          <span className="qcard__opt-label">Upload anyway</span>
          <span className="qcard__opt-desc">Store copies on Möbius ({size}).</span>
        </span>
      </button>
    </div>
  )
}

export default function SharedFolderUploadPrompt({ pending, onUploadAnyway, onCancel, onShared }) {
  const { canRemember } = useSharedFolders()
  const { error, visitInput, share, onVisitFiles } = useShareFolder(canRemember, onShared)
  if (!pending) return null

  const size = copyByteLabel(pending.bytes)
  const files = pending.count === 1 ? '1 file' : `${pending.count} files`
  return (
    <section className="qcard upload-folder-prompt" aria-label="Big upload">
      <div className="upload-folder-prompt__head">
        <span className="qcard__header">Big upload</span>
        <button type="button" className="upload-folder-prompt__close" onClick={onCancel} aria-label="Cancel this upload">
          <X width={16} height={16} aria-hidden="true" />
        </button>
      </div>
      <p className="qcard__text">
        {files} · {size}. Share their folder instead? The files stay on your device and use no space on Möbius.
      </p>
      <UploadChoices
        canRemember={canRemember}
        size={size}
        onShare={() => { void share() }}
        onUploadAnyway={onUploadAnyway}
      />
      {error && <p className="upload-folder-prompt__error" role="alert">{error}</p>}
      <input
        ref={visitInput}
        className="upload-folder-prompt__visit-input"
        type="file"
        webkitdirectory=""
        multiple
        tabIndex={-1}
        aria-hidden="true"
        onChange={onVisitFiles}
      />
    </section>
  )
}
