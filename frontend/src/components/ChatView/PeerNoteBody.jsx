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

/** The ways to read one note whole: through each chat that can see it. */
export function noteReaders(chatIds, messageId) {
  if (!messageId) return []
  return (Array.isArray(chatIds) ? chatIds : [chatIds]).filter(Boolean).map(chatId => async () => (
    await jsonOrThrow(await api.agentCoordination.message(chatId, messageId), 'Peer note failed:')
  ).body)
}

export function FullNoteText({ text }) {
  const [shown, setShown] = useState(REVEAL_CHARS)
  return <>
    <span className="peer-note-full">{text.slice(0, shown)}</span>
    {shown < text.length && <button type="button" className="peer-note-more" onClick={() => setShown(shown + REVEAL_CHARS)}>
      Show more ({(text.length - shown).toLocaleString()} characters left)
    </button>}
  </>
}

// `readers` (see noteReaders) read the whole text; `noun` names it ("note",
// or "text" for a dependency's claim texts).
export default function PeerNoteBody({ readers = [], body, truncated, render, noun = 'note' }) {
  const note = useFullPeerNote(readers)
  if (note.full != null) {
    return note.full.length > PEER_NOTE_CHARS ? <FullNoteText text={note.full} /> : (note.full && render(note.full))
  }
  return <>
    {body && render(body)}
    {truncated && <ReadFullAction note={note} noun={noun} />}
  </>
}

/** The "Show full …" action for a text shown in part (or its label when it
 *  cannot be read here); `note` is useFullPeerNote's state. */
export function ReadFullAction({ note, noun = 'note' }) {
  if (!note.readable) return <span className="peer-note-excerpt">{`Excerpt — full ${noun} not shown`}</span>
  return <button type="button" className="peer-note-more" disabled={note.state === 'loading'} onClick={note.load}>
    {note.state === 'loading' ? `Loading full ${noun}…` : note.state === 'error' ? `Could not load the full ${noun}. Retry` : `Show full ${noun}`}
  </button>
}
