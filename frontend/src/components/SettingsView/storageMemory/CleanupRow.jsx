/* One "You can clear" row: open the confirm step at once, fill in its size, then clear. */
import { useId, useRef, useState } from 'react'
import ResourceConfirmDrawer from './ResourceConfirmDrawer.jsx'
import { CLEANUP_COPY, formatBytes, previewSentence, resultSentence, storedSize } from './storageMemoryPresentation.js'

// The row size is stored space; the confirm step measures what Clear frees,
// so Clear stays available even when the stored size is unknown.
function storedLabel(item) {
  const { value, note } = storedSize(item)
  return <>{value} <small>{note}</small></>
}

// The confirm step opens immediately with its fixed words; only the size and
// the Clear button wait for the measurement, so nothing jumps when it lands.
function ConfirmPanel({ target, step, onCancel, onConfirm, cancelRef }) {
  const copy = CLEANUP_COPY[target]
  const measuring = step.phase === 'measuring'
  const clearing = step.phase === 'clearing'
  const bytes = step.preview?.bytes || 0
  return (
    <div className="storage-memory__confirm" role="group" aria-label={`Confirm: clear ${copy.label}`} aria-busy={measuring || clearing}>
      <p className="storage-memory__confirm-lead" role="status">
        {measuring ? 'Checking how much this frees…' : previewSentence(target, step.preview)}
      </p>
      {copy.context && <p className="storage-memory__note">{copy.context}</p>}
      <dl className="storage-memory__confirm-facts">
        <dt>Deletes</dt><dd>{copy.deleted}</dd>
        <dt>Keeps</dt><dd>{copy.kept}</dd>
        <dt>After</dt><dd>{copy.after}</dd>
      </dl>
      <div className="storage-memory__confirm-actions">
        <button ref={cancelRef} type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={onCancel} disabled={clearing}>Cancel</button>
        <button type="button" className="settings__btn settings__btn--sm storage-memory__confirm-clear" onClick={onConfirm} disabled={measuring || clearing || !bytes}>
          {measuring ? 'Checking…' : clearing ? 'Clearing…' : `Clear ${formatBytes(bytes)}`}
        </button>
      </div>
    </div>
  )
}

export default function CleanupRow({ item, resources }) {
  const [step, setStep] = useState({ phase: 'idle' })
  // The last confirm content stays mounted while the drawer slides shut, so
  // closing animates instead of snapping; CSS hides it once fully closed.
  const [panelStep, setPanelStep] = useState(null)
  const copy = CLEANUP_COPY[item.target]
  const drawerOpen = ['measuring', 'confirm', 'clearing'].includes(step.phase)
  const outcome = step.phase === 'done' || step.phase === 'failed'
  const cancelRef = useRef(null)
  const openerRef = useRef(null)
  const outcomeRef = useRef(null)
  const previewRequest = useRef(0)
  const drawerId = useId()

  function show(next) {
    setStep(next)
    if (['measuring', 'confirm', 'clearing'].includes(next.phase)) setPanelStep(next)
  }

  async function open() {
    const request = ++previewRequest.current
    show({ phase: 'measuring' })
    try {
      const preview = await resources.preview(item.target)
      if (request === previewRequest.current) show({ phase: 'confirm', preview })
    } catch (err) {
      if (request === previewRequest.current) show({ phase: 'failed', message: err?.message || 'Could not measure this now.' })
    }
  }

  function cancel() {
    previewRequest.current += 1
    setStep({ phase: 'idle' })
  }

  async function clear() {
    if (step.phase !== 'confirm') return
    show({ ...step, phase: 'clearing' })
    try {
      const result = await resources.clear(item.target)
      show({ phase: 'done', message: resultSentence(result.cleanup, result.resources?.storage) })
    } catch (err) {
      show({ phase: 'failed', message: err?.message || 'The cleanup did not finish.' })
    }
  }

  return (
    <li className={`storage-memory__item${drawerOpen ? ' storage-memory__item--open' : ''}`}>
      <span className="storage-memory__item-name">{copy.label}</span>
      <span className="storage-memory__size storage-memory__stored">{storedLabel(item)}</span>
      <span className="storage-memory__item-action">
        <button ref={openerRef} type="button" className="settings__btn settings__btn--outline settings__btn--sm"
          onClick={open} disabled={drawerOpen} aria-expanded={drawerOpen}
          aria-controls={drawerId} aria-label={`Clear ${copy.label.toLowerCase()}`}>
          Clear
        </button>
      </span>
      <span ref={outcomeRef} tabIndex={-1} className={`storage-memory__item-sub${outcome ? ` storage-memory__item-sub--${step.phase}` : ''}`} role={outcome ? 'status' : undefined}>
        {outcome ? step.message : copy.description}
      </span>
      <ResourceConfirmDrawer id={drawerId} open={drawerOpen} busy={step.phase === 'clearing'}
        initialFocusRef={cancelRef} restoreFocusRef={outcome ? outcomeRef : openerRef} onCancel={cancel}>
          {panelStep && (
            <ConfirmPanel target={item.target} step={panelStep}
              onCancel={cancel} onConfirm={clear} cancelRef={cancelRef} />
          )}
      </ResourceConfirmDrawer>
    </li>
  )
}
