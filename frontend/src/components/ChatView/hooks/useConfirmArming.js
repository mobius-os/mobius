/* When the host-drawn Confirm may be pressed.

   A *showing* is one presentation of one exact confirmation: document
   generation, action key and app list. It ends when any of those changes,
   when the list disappears, and on every owner event (Confirm included) or
   release, so no earlier arming is ever reused.

   Within a showing, Confirm arms only once
   - the end of the app's list (a sentinel after its last item) has been on
     screen at least once during this showing, and
   - Confirm itself is on screen in a visible page,
   and the second condition has then held continuously for CONFIRM_ARM_MS.
   Both observations arrive as `{ showing, visible }` and count only for the
   showing they name: an element's position under an earlier list (the same
   sentinel before 256 items pushed it down) can never arm a new showing.
   The list is drawn below Confirm, so its end having been on screen means
   the whole list has passed through the viewport; a long list just needs
   scrolling. A double-click, a swapped list or an unseen list never arms. */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

export const CONFIRM_ARM_MS = 500

export function confirmationIdentity(state) {
  const action = state?.actions.find(item => item.confirming && item.confirmation)
  return action ? JSON.stringify([action.key, action.confirmation]) : null
}

const seenFor = (view, showing) => Boolean(showing) && view?.showing === showing && view.visible === true

export default function useConfirmArming({ state, generation, supported, confirmView, listEndView }) {
  const identity = confirmationIdentity(state)
  const [disarms, setDisarms] = useState(0)
  // Unique per showing: a disarm or new generation changes it even when the
  // same list comes back.
  const showing = identity && supported === true ? `${generation}:${disarms}:${identity}` : null
  const action = showing ? JSON.parse(identity)[0] : null

  // Callbacks read what is committed, never a value written during render.
  const committed = useRef({ showing: null, action: null, identity: null, recoveryPending: false })
  useLayoutEffect(() => {
    committed.current = { showing, action, identity, recoveryPending: state?.recoveryPending === true }
  }, [showing, action, identity, state?.recoveryPending])

  const [endSeen, setEndSeen] = useState(null)
  const listEndVisible = seenFor(listEndView, showing)
  const ready = Boolean(showing) && endSeen === showing && seenFor(confirmView, showing)

  const [armed, setArmed] = useState(null)
  const armedRef = useRef(null)
  useEffect(() => {
    armedRef.current = null
    setArmed(null)
    if (!ready) return undefined
    const timer = setTimeout(() => {
      armedRef.current = showing
      setArmed(showing)
    }, CONFIRM_ARM_MS)
    return () => clearTimeout(timer)
  }, [ready, showing])
  // Latch "the list end was on screen" for this showing. (After the arming
  // effect, so the render this latch causes is the one that starts the timer.)
  useEffect(() => {
    if (showing && listEndVisible && endSeen !== showing) setEndSeen(showing)
  }, [showing, listEndVisible, endSeen])

  /** End the showing now, before React re-renders. */
  const disarm = useCallback(() => {
    armedRef.current = null
    setDisarms(count => count + 1)
  }, [])

  /** A parsed report: one that changes or removes the shown list ends the showing. */
  const noteReport = useCallback(safe => {
    const held = safe.checkpointInvalid || safe.recoveryError || committed.current.recoveryPending
    if ((held ? null : confirmationIdentity(safe)) !== committed.current.identity) disarm()
  }, [disarm])

  /** Whether a `confirm` for `key` may be sent now. */
  const mayConfirm = useCallback(key => {
    const current = committed.current
    return Boolean(current.showing) && armedRef.current === current.showing && current.action === key
  }, [])

  return { armed: ready && armed === showing, showing, disarm, noteReport, mayConfirm }
}
