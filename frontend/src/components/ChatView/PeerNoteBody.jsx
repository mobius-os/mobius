/* PeerNoteBody shows a peer note and reads a truncated one whole on request. */
import { useState } from 'react'
import { api, jsonOrThrow } from '../../api/client.js'
import './PeerNoteBody.css'

// Views carry only a note's head; one owner read returns the rest.
export default function PeerNoteBody({ chatId, messageId, body, truncated, render, className = 'chat__peer-excerpt' }) {
  const [full, setFull] = useState(null)
  const [state, setState] = useState('idle')
  const text = full ?? body
  const readable = truncated && !full && chatId && messageId
  const load = async () => {
    setState('loading')
    try {
      const note = await jsonOrThrow(await api.agentCoordination.message(chatId, messageId), 'Peer note failed:')
      setFull(note.body)
      setState('idle')
    } catch {
      setState('error')
    }
  }
  return <>
    {text && render(text)}
    {readable && <button type="button" className="peer-note-more" disabled={state === 'loading'} onClick={load}>
      {state === 'loading' ? 'Loading full note…' : state === 'error' ? 'Could not load the full note. Retry' : 'Show full note'}
    </button>}
    {truncated && !full && !readable && <span className={className}>Excerpt — full note not shown</span>}
  </>
}
