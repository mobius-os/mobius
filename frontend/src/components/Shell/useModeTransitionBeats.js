import { useCallback, useEffect, useRef, useState } from 'react'

// The two transient builder mode-transition "beats" — the ENTER deal (single ->
// panes: the strip deals in, the single pane lift-settles) and the EXIT reverse
// card-deal (a multi-pane exit holds the tiled render for one deal before
// collapsing to single) — owned as ONE mutually-exclusive state machine.
//
// Why one hook, one timer, one arm() instead of two independent boolean latches
// (the earlier shape): the beats share the effectiveViewMode override in Shell
// (builderExiting holds the tiled render while the reducer viewMode is already
// 'single'), so a beat that OUTLIVES the transition that armed it wedges the
// render — the workspace stays tiled after an exit even though the mode flipped.
// A rapid re-ENTER within the 250ms exit beat exposed it: the old code left
// builderExiting true (its timer still pending) while ALSO arming builderEntering,
// so both root classes fought (deal-out over deal-in) AND renderTabRects wrongly
// widened the focused pane to full width during the entry deal.
//
// INVARIANT: at most ONE beat is ever active, and a beat NEVER survives the next
// mode toggle. Every toggle calls armBeat('enter' | 'exit' | null) exactly once;
// arming a beat cancels the opposite one (state + timer) in the same commit, and
// armBeat(null) — reduced motion, or a single-leaf instant collapse — clears
// both. So no sequence of toggles can strand a beat: the beat state is a pure
// function of the most recent armBeat call, self-cleared on its own deadline.
//
// `scheduler` is injected only so the state machine is unit-testable against a
// mock clock; it defaults to the real timers.
export function useModeTransitionBeats({ enterMs, exitMs, scheduler } = {}) {
  const set = scheduler?.set || setTimeout
  const clear = scheduler?.clear || clearTimeout
  const [entering, setEntering] = useState(false)
  const [exiting, setExiting] = useState(false)
  const timerRef = useRef(0)
  // Cancel a beat in flight if Shell unmounts so its timer can't set state on a
  // dead component. Only ever clears on unmount (empty deps).
  useEffect(() => () => clear(timerRef.current), [clear])

  const armBeat = useCallback((kind) => {
    clear(timerRef.current)
    // Set BOTH beats every call so arming one always cancels the other and
    // `null` clears both — the mutual-exclusion + no-stale-beat guarantee lives
    // here, in one place, rather than scattered across the toggle handler.
    setEntering(kind === 'enter')
    setExiting(kind === 'exit')
    if (kind) {
      timerRef.current = set(() => {
        setEntering(false)
        setExiting(false)
      }, kind === 'exit' ? exitMs : enterMs)
    }
  }, [set, clear, enterMs, exitMs])

  return { builderEntering: entering, builderExiting: exiting, armBeat }
}
