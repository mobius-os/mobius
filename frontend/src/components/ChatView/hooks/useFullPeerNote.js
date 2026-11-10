/* useFullPeerNote reads one truncated peer note whole, on request. */
import { useState } from 'react'

// The chats are tried in order: a Project view can read a note through either
// end of it, and one end may since have been deleted (404). When none can read
// it any more the note is "unavailable": retrying could never succeed.
export default function useFullPeerNote(chatIds, messageId, read) {
  const [full, setFull] = useState(null)
  const [state, setState] = useState('idle')
  const candidates = (Array.isArray(chatIds) ? chatIds : [chatIds]).filter(Boolean)
  const load = async () => {
    setState('loading')
    for (const chatId of candidates) {
      try {
        const note = await read(chatId, messageId)
        setFull(note.body)
        setState('idle')
        return
      } catch (error) {
        if (error?.status !== 404) {
          setState('error')
          return
        }
      }
    }
    setState('unavailable')
  }
  return {
    full, state, load,
    readable: Boolean(messageId && candidates.length) && state !== 'unavailable',
  }
}
