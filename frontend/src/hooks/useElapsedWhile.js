import { useEffect, useState } from 'react'

/**
 * Milliseconds since `running` last became true, re-rendering once a second
 * while it stays true; 0 while it is false.
 *
 * A visible clock is what tells a long, healthy operation apart from a stuck one.
 */
export default function useElapsedWhile(running) {
  const [startedAt, setStartedAt] = useState(null)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!running) {
      setStartedAt(null)
      return undefined
    }
    const start = Date.now()
    setStartedAt(start)
    setNow(start)
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [running])
  return running && startedAt !== null ? now - startedAt : 0
}
