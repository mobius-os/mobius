/* Running chats and their Stop controls. */
import { useId, useRef, useState } from 'react'
import StatusDot from '../../ui/StatusDot.jsx'
import ResourceConfirmDrawer from './ResourceConfirmDrawer.jsx'
import { GROUP_COPY, runningTurnTitle } from './storageMemoryPresentation.js'

function StopConfirmation({ turn, phase, error, cancelRef, onCancel, onConfirm }) {
  const stopping = phase === 'stopping'
  return (
    <div className="storage-memory__confirm" role="group" aria-label={`Confirm stop: ${turn.title}`} aria-busy={stopping}>
      <p className="storage-memory__confirm-lead">Stop this turn?</p>
      <p className="storage-memory__note">Ends the current agent reply in “{turn.title}”.</p>
      {turn.helpers > 0 && <p className="storage-memory__note">{turn.helpers === 1 ? 'Its helper agent stops too.' : `Its ${turn.helpers} helper agents stop too.`}</p>}
      <p className="storage-memory__note">Messages stay. Unfinished work may need checking before you resume.</p>
      {error && <p className="storage-memory__item-sub--failed" role="alert">{error}</p>}
      <div className="storage-memory__confirm-actions">
        <button ref={cancelRef} type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={onCancel} disabled={stopping}>Cancel</button>
        <button type="button" className="settings__btn settings__btn--outline settings__btn--sm storage-memory__stop"
          onClick={onConfirm} disabled={stopping} aria-label={`Confirm stop: ${turn.title}`}>{stopping ? 'Stopping…' : 'Stop turn'}</button>
      </div>
    </div>
  )
}

function RunningTurnRow({ turn, onStop, onOpenChat }) {
  const [phase, setPhase] = useState('idle')
  const [error, setError] = useState('')
  const cancelRef = useRef(null)
  const openerRef = useRef(null)
  const drawerId = useId()
  const open = phase !== 'idle'
  async function stop() {
    setPhase('stopping'); setError('')
    try { await onStop(turn); setPhase('idle') }
    catch (err) { setPhase('failed'); setError(err?.message || 'Could not stop this turn. Try again.') }
  }
  // Stop clears the chat's queued messages and only the chat screen sends
  // them again, so a chat with a queue is stopped from there.
  const queued = turn.queued > 0
  return (
    <li className="storage-memory__turn">
      <div className="storage-memory__turn-main">
        <span className="storage-memory__turn-name">{turn.title}</span>
        {!queued && <button ref={openerRef} type="button" className="settings__btn settings__btn--outline settings__btn--sm"
          disabled={open} onClick={() => setPhase('confirm')} aria-label={`Stop turn: ${turn.title}`} aria-expanded={open} aria-controls={drawerId}>Stop turn</button>}
        <div className="storage-memory__turn-meta">
          <StatusDot color="--green">{phase === 'stopping' ? 'Stopping…' : 'Running'}</StatusDot>
          {onOpenChat && <button type="button" className="storage-memory__chat-link" onClick={() => onOpenChat(turn.chat_id)} aria-label={`Open chat: ${turn.title}`}>{queued ? 'Open chat to stop' : 'Open chat'}</button>}
        </div>
        {queued && <p className="storage-memory__note storage-memory__turn-queued">Has queued messages, so stop it in the chat to keep them.</p>}
      </div>
      <ResourceConfirmDrawer id={drawerId} open={open} busy={phase === 'stopping'} initialFocusRef={cancelRef} restoreFocusRef={openerRef} onCancel={() => setPhase('idle')}>
        <StopConfirmation turn={turn} phase={phase} error={error} cancelRef={cancelRef} onCancel={() => setPhase('idle')} onConfirm={stop} />
      </ResourceConfirmDrawer>
    </li>
  )
}

export default function RunningNow({ running, onStop, onOpenChat }) {
  const [stoppedTitle, setStoppedTitle] = useState('')
  const headingRef = useRef(null)
  async function stop(turn) {
    await onStop(turn.chat_id)
    setStoppedTitle(turn.title)
    headingRef.current?.focus({ preventScroll: true })
  }
  return (
    <section className="storage-memory__group" data-resource-group="running" tabIndex={-1} aria-labelledby="storage-running-title">
      <div className="storage-memory__group-head">
        <div className="storage-memory__group-text">
          <h3 ref={headingRef} tabIndex={-1} id="storage-running-title" className="storage-memory__group-title">{GROUP_COPY.running.title}</h3>
          <p className="storage-memory__group-helper">{GROUP_COPY.running.helper}</p>
        </div>
        <span className="storage-memory__count">{running.length} {running.length === 1 ? 'turn' : 'turns'}</span>
      </div>
      {stoppedTitle && <p className="storage-memory__note" role="status">Stopped “{stoppedTitle}”. Your messages stay.</p>}
      {running.length ? <ul className="storage-memory__turns">{running.map(turn => <RunningTurnRow key={turn.chat_id} turn={{ ...turn, title: runningTurnTitle(turn.title) }} onStop={stop} onOpenChat={onOpenChat} />)}</ul>
        : <p className="storage-memory__empty">No agents running</p>}
    </section>
  )
}
