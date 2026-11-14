/* The shell's own confirmation of a sensitive app action, drawn over the app's
   pane and outside its frame. Everything the owner reads here is the server's
   text (rendered as text) plus the app's name from the shell's own state; the
   app supplies identifiers only. It shares the file-export dialog's card. */
import { useEffect, useId, useRef, useState } from 'react'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { fileExportInk } from '../../lib/appFileExport.js'
import { OWNER_GRANT_ARM_MS } from '../../lib/appOwnerGrant.js'

// The verb button stays inert until what it confirms has been on screen for
// OWNER_GRANT_ARM_MS, and a tap counts only if its pointerdown and pointerup
// both came after that, so a sheet that appears under a finger cannot be
// confirmed. A keyboard click carries no pointer events (detail 0).
function useArmedConfirm(shownKey, onConfirm) {
  const [armedFor, setArmedFor] = useState(null)
  const armedKeyRef = useRef(null)
  const pressedArmedRef = useRef(false)
  useEffect(() => {
    if (!shownKey) return undefined
    const timer = setTimeout(() => {
      armedKeyRef.current = shownKey
      setArmedFor(shownKey)
    }, OWNER_GRANT_ARM_MS)
    return () => {
      clearTimeout(timer)
      armedKeyRef.current = null
      pressedArmedRef.current = false
    }
  }, [shownKey])
  const isArmed = () => armedKeyRef.current !== null
  return {
    armed: !!shownKey && armedFor === shownKey,
    pressHandlers: {
      onPointerDown() { pressedArmedRef.current = isArmed() },
      onPointerUp() { pressedArmedRef.current &&= isArmed() },
      onPointerCancel() { pressedArmedRef.current = false },
      onClick(event) {
        const counts = isArmed() && (event.detail === 0 || pressedArmedRef.current)
        pressedArmedRef.current = false
        if (counts) onConfirm()
      },
    },
  }
}

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
  const dialogRef = useRef(null)
  const cancelRef = useRef(null)
  const titleId = useId()
  const { phase, preview, notice, error } = request
  const showsFacts = phase === 'ready' || phase === 'confirming'
  const { armed, pressHandlers } = useArmedConfirm(phase === 'ready' ? preview : null, onConfirm)
  useDialogFocus({ containerRef: dialogRef, initialFocusRef: cancelRef, onClose: onCancel })
  const title = showsFacts ? preview.headline : TITLES[phase]
  const verb = WAITING_VERBS[phase] || preview?.verb
  const cautious = showsFacts && preview.tone === 'caution'
  return (
    <div className="canvas-file-export canvas-owner-confirm" role="dialog" aria-modal="true"
      aria-labelledby={titleId} ref={dialogRef} tabIndex={-1}>
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
        <div className="canvas-file-export__actions">
          <button type="button" ref={cancelRef} onClick={onCancel}>
            {phase === 'error' ? 'Close' : 'Cancel'}
          </button>
          {phase !== 'error' && (
            <button type="button" disabled={!armed} aria-disabled={!armed} {...pressHandlers}
              className={`canvas-file-export__primary${cautious ? ' canvas-owner-confirm__caution' : ''}`}>
              {verb}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
