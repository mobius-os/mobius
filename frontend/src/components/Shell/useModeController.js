import { useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef } from 'react'
import { initialModeState, modeReducer } from './modeMachine.js'

// Synchronizes the durable workspace mode with the one transient state that still
// belongs in React: drag-preview. Visual mode motion is a separate browser scene
// transaction; this controller never watches CSS animations or schedules recovery.
export default function useModeController({ committedMode }) {
  const [state, dispatch] = useReducer(
    modeReducer,
    undefined,
    () => initialModeState(committedMode),
  )
  const stateRef = useRef(state)
  useLayoutEffect(() => { stateRef.current = state }, [state])

  // The workspace reducer is the sole authority for committed mode. Its
  // synchronous transition boundary calls this with the reducer's ACTUAL
  // result; request intent must never predict or mirror a mode change.
  const syncCommitted = useCallback((nextMode) => {
    const event = { type: 'sync-committed', committedMode: nextMode }
    stateRef.current = modeReducer(stateRef.current, event)
    dispatch(event)
  }, [])

  useEffect(() => {
    syncCommitted(committedMode)
  }, [committedMode, syncCommitted])

  const dragArm = useCallback(() => {
    const current = stateRef.current
    const id = current.committedMode === 'single' ? current.nextId : null
    dispatch({ type: 'drag-arm' })
    return id
  }, [])
  const dragCancel = useCallback((id) => { dispatch({ type: 'drag-cancel', id }) }, [])

  return useMemo(() => ({
    state,
    syncCommitted,
    dragArm,
    dragCancel,
  }), [state, syncCommitted, dragArm, dragCancel])
}
