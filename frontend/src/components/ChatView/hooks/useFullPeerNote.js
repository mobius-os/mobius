/* useFullPeerNote reads one truncated text whole, on request. */
import { useState } from 'react'

// `readers` are the ways to read it, tried in order: a Project view can read a
// note through either end of it, and one end may since have been deleted
// (404). Each resolves to the whole text.
export default function useFullPeerNote(readers) {
  const [full, setFull] = useState(null)
  const [state, setState] = useState('idle')
  const load = async () => {
    setState('loading')
    for (const read of readers) {
      try {
        setFull(await read())
        setState('idle')
        return
      } catch (error) {
        if (error?.status !== 404) break
      }
    }
    setState('error')
  }
  return { full, state, load, readable: readers.length > 0 }
}
