/* PeerNoteBody shows a peer note and reads a truncated one whole on request. */
import { useState } from 'react'
import { api, jsonOrThrow } from '../../api/client.js'
import useFullPeerNote from './hooks/useFullPeerNote.js'
import { PEER_NOTE_CHARS } from './peerMessageCard.js'
import './PeerNoteBody.css'

// Views carry only a note's head, within PEER_NOTE_CHARS. A full note beyond
// it is shown as plain text, revealed a page at a time, so a very long note
// never freezes the view.
const REVEAL_CHARS = 20_000

async function readNote(chatId, messageId) {
  return jsonOrThrow(await api.agentCoordination.message(chatId, messageId), 'Peer note failed:')
}

export function FullNoteText({ text }) {
  const [shown, setShown] = useState(REVEAL_CHARS)
  return <>
    <div className="peer-note-full">{text.slice(0, shown)}</div>
    {shown < text.length && <button type="button" className="peer-note-more" onClick={() => setShown(shown + REVEAL_CHARS)}>
      Show more ({(text.length - shown).toLocaleString()} characters left)
    </button>}
  </>
}

export default function PeerNoteBody({ chatId, messageId, body, truncated, render, read = readNote }) {
  const note = useFullPeerNote(chatId, messageId, read)
  if (note.full != null) {
    return note.full.length > PEER_NOTE_CHARS ? <FullNoteText text={note.full} /> : (note.full && render(note.full))
  }
  return <>
    {body && render(body)}
    {truncated && note.readable && <button type="button" className="peer-note-more" disabled={note.state === 'loading'} onClick={note.load}>
      {note.state === 'loading' ? 'Loading full note…' : note.state === 'error' ? 'Could not load the full note. Retry' : 'Show full note'}
    </button>}
    {truncated && !note.readable && <span className="peer-note-excerpt">Excerpt — full note not shown</span>}
  </>
}
