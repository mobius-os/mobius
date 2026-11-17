/* The shell's own confirmation of a sensitive app action, drawn over the app's
   pane and outside its frame. Everything the owner reads here is the server's
   text (rendered as text) plus the app's name from the shell's own state; the
   app supplies identifiers only. It shares the file-export dialog's card. */
import { useId, useRef } from 'react'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { fileExportInk } from '../../lib/appFileExport.js'
import useKeyAnswerGuard, { earlyAnswerHint } from '../SettingsView/useKeyAnswerGuard.js'

function OwnerConfirmFacts({ preview, notice }) {
  return (
    <>
      {notice && <p className="canvas-owner-confirm__notice" role="status">{notice}</p>}
      <dl className="canvas-owner-confirm__facts">
        {(preview.facts || []).map(({ label, value }, index) => (
          <div className="canvas-owner-confirm__fact" key={`${index}:${label}`}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </>
  )
}

const TITLES = { checking: 'Checking this request…', error: 'Can’t confirm this' }
const WAITING_VERBS = { checking: 'Checking…', confirming: 'Confirming…' }

export default function OwnerConfirmSheet({ appName, themeColor, request, onCancel, onConfirm }) {
  const cancelRef = useRef(null)
  const titleId = useId()
  const { phase, preview, notice, error } = request
  const showsFacts = phase === 'ready' || phase === 'confirming'
  // The verb wakes only once what it confirms has been seen, still and
  // untouched for a moment, as on a key request card, so a sheet that
  // appears or is revealed under a finger or a key cannot be confirmed.
  const guard = useKeyAnswerGuard({ enabled: phase === 'ready' })
  const armed = phase === 'ready' && guard.active
  useDialogFocus({ containerRef: guard.cardProps.ref, initialFocusRef: cancelRef, onClose: onCancel })
  const title = showsFacts ? preview.headline : TITLES[phase]
  const verb = WAITING_VERBS[phase] || preview?.verb
  const cautious = showsFacts && preview.tone === 'caution'
  return (
    <div className="canvas-file-export canvas-owner-confirm" role="dialog" aria-modal="true"
      aria-labelledby={titleId} tabIndex={-1} {...guard.cardProps}>
      <div className="canvas-file-export__card" style={themeColor ? {
        '--file-export-accent': themeColor,
        '--file-export-ink': fileExportInk(themeColor),
      } : undefined}>
        <div className="canvas-file-export__eyebrow">{appName || 'This app'} asks you to confirm</div>
        <h2 id={titleId}>{title}</h2>
        <div aria-live="polite">
          {showsFacts && <OwnerConfirmFacts preview={preview} notice={notice} />}
          {phase === 'checking' && <p>Möbius is asking the server what this will do. Nothing happens until you confirm.</p>}
          {phase === 'error' && <p className="canvas-owner-confirm__error" role="alert">{error}</p>}
        </div>
        {guard.early !== null && <p className="canvas-owner-confirm__notice" role="status">{earlyAnswerHint(guard.early)}</p>}
        <div className="canvas-file-export__actions">
          <button type="button" ref={cancelRef} onClick={onCancel}>
            {phase === 'error' ? 'Close' : 'Cancel'}
          </button>
          {phase !== 'error' && (
            <button type="button" disabled={!armed} aria-disabled={!armed}
              onClick={event => { if (guard.counts(event)) onConfirm() }}
              className={`canvas-file-export__primary${cautious ? ' canvas-owner-confirm__caution' : ''}`}>
              {verb}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
