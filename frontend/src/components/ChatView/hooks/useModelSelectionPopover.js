import { useEffect, useRef, useState } from 'react'


export default function useModelSelectionPopover(
  modelSelectionRequest,
  composerInputRef,
) {
  const [mode, setMode] = useState(null)
  const wasInputFocusedRef = useRef(false)
  const handledRequestRef = useRef(modelSelectionRequest)

  useEffect(() => {
    if (!modelSelectionRequest || handledRequestRef.current === modelSelectionRequest) {
      return
    }
    handledRequestRef.current = modelSelectionRequest
    wasInputFocusedRef.current = document.activeElement === composerInputRef?.current
    setMode('model')
  }, [composerInputRef, modelSelectionRequest])

  return { mode, setMode, wasInputFocusedRef }
}
