/* One helper conversation preview per chat pane. A docked preview leaves the
   chat usable, so another helper row can be opened while one is showing: the
   pane owns which caller's preview is open, and opening another replaces it. */
import { createContext, useCallback, useContext, useEffect, useId, useMemo, useRef, useState } from 'react'

const HelperPreviewSlotContext = createContext(null)

export function HelperPreviewProvider({ children }) {
  const [owner, setOwner] = useState(null)
  // claim and release keep their identity, so a caller's cleanup never re-runs
  // (and gives the preview up) just because another caller opened one.
  const claim = useCallback(next => setOwner(next), [])
  const release = useCallback(previous => setOwner(current => current === previous ? null : current), [])
  const pane = useMemo(() => ({ owner, claim, release }), [owner, claim, release])
  return <HelperPreviewSlotContext.Provider value={pane}>{children}</HelperPreviewSlotContext.Provider>
}

/* useState for a caller's open preview: [value, setValue]. The value shows only
   while this caller holds its pane's one preview; setting a value takes it over
   from any other caller, and `null` or unmounting gives it up. The caller keeps
   its own value, so it may refresh it while rendering. Outside a provider
   (isolated renders) it is plain local state. */
export function useHelperPreview() {
  const id = useId()
  const pane = useContext(HelperPreviewSlotContext)
  const [local, setLocal] = useState(null)
  const holds = !pane || pane.owner === id
  const holdsRef = useRef(holds)
  holdsRef.current = holds
  const claim = pane?.claim
  const release = pane?.release
  const setValue = useCallback(next => {
    setLocal(next)
    // Only an open from an event handler claims; a caller refreshing the value
    // it already shows holds the pane already.
    if (claim && next != null && !holdsRef.current) {
      holdsRef.current = true
      claim(id)
    }
  }, [claim, id])
  const closed = local == null
  useEffect(() => { if (closed) release?.(id) }, [closed, release, id])
  useEffect(() => release && (() => release(id)), [release, id])
  return [holds ? local : null, setValue]
}
