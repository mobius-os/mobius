/* Read-only view of the files this chat changed. */

import { useCallback, useRef, useState } from 'react'
import { X } from '@openai/apps-sdk-ui/components/Icon'
import useContextMenuOutsideDismiss from '../../hooks/useContextMenuOutsideDismiss.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { formatRelativeTime } from '../../lib/relativeTime.js'
import FileDiffList from '../DiffView/FileDiffList.jsx'
import { useChatChanges } from './useChatChanges.js'
import './ChatWork.css'

export default function ChatDiffViewer({
  chatId,
  initialEntries,
  onClose,
  returnFocusRef,
}) {
  const changes = useChatChanges(chatId, initialEntries)
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  const restoreFocusGuardRef = useRef(true)
  const expansionSequenceRef = useRef(0)
  const [expansionCommand, setExpansionCommand] = useState(null)

  const dismissFromOutside = useCallback(() => {
    // The outside press belongs to the destination beneath this
    // pointer-transparent panel. Do not pull focus back to the Changes trigger
    // after that destination has already started taking ownership.
    restoreFocusGuardRef.current = false
    onClose?.()
  }, [onClose])
  const shouldRestoreFocus = useCallback(
    () => restoreFocusGuardRef.current !== false,
    [],
  )

  useContextMenuOutsideDismiss({
    open: true,
    menuRef: dialogRef,
    onDismiss: dismissFromOutside,
  })

  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: closeRef,
    restoreFocusRef: returnFocusRef,
    shouldRestoreFocus,
    onClose,
    modal: false,
    lockScroll: false,
  })

  function setEveryDiffExpanded(expanded) {
    expansionSequenceRef.current += 1
    setExpansionCommand({ id: expansionSequenceRef.current, expanded })
  }

  const { groups, excerptCount, latestTs } = changes
  const fileCount = changes.files.length

  return (
    <div className="chat-work__overlay" role="presentation">
      <div
        ref={dialogRef}
        className="chat-work"
        role="dialog"
        aria-labelledby="chat-work-diff-title"
      >
        <header className="chat-work__head">
          <div>
            <h2 id="chat-work-diff-title">Changes from this chat</h2>
            {fileCount > 0 ? (
              <p>
                {fileCount} {fileCount === 1 ? 'file' : 'files'}
                {latestTs ? ` · last edit ${formatRelativeTime(new Date(latestTs).toISOString())}` : ''}
              </p>
            ) : null}
          </div>
          <div className="chat-work__head-actions">
            {groups.length > 0 ? (
              <button
                type="button"
                onClick={() => setEveryDiffExpanded(expansionCommand?.expanded !== true)}
              >
                {expansionCommand?.expanded === true ? 'Collapse all' : 'Expand all'}
              </button>
            ) : null}
            <button ref={closeRef} type="button" className="chat-work__close" onClick={onClose} aria-label="Close changes">
              <X width={19} height={19} />
            </button>
          </div>
        </header>

        <div className="chat-work__body">
          {changes.loading && groups.length === 0 ? (
            <p className="chat-work__state" role="status">Loading changes…</p>
          ) : changes.error && groups.length === 0 ? (
            <p className="chat-work__state chat-work__state--error" role="alert">Could not read this chat’s change history.</p>
          ) : groups.length === 0 ? (
            <div className="chat-work__empty">
              <strong>No file changes yet</strong>
              <span>Files this chat edits will be listed here.</span>
            </div>
          ) : (
            <div className="chat-work__updates">
              {changes.error ? <p className="chat-work__notice">Showing the changes already loaded in this chat.</p> : null}
              {excerptCount > 0 ? <p className="chat-work__notice">{excerptCount} older {excerptCount === 1 ? 'edit is' : 'edits are'} excerpt-only.</p> : null}
              {groups.map(group => (
                <section className="chat-work__update" key={group.id}>
                  <div className="chat-work__update-head">
                    <div>
                      <span className="chat-work__update-number">{group.label}</span>
                      <strong>{group.files.length} {group.files.length === 1 ? 'file' : 'files'}</strong>
                    </div>
                  </div>
                  <FileDiffList
                    files={group.files}
                    diffTruncated={excerptCount > 0}
                    expansionCommand={expansionCommand}
                  />
                </section>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
