import { useEffect, useRef, useState } from 'react'


export default function useModelSelectionPopover(
  modelSelectionRequest,
  composerInputRef,
) {
  const [open, setOpen] = useState(false)
  const wasInputFocusedRef = useRef(false)
  const handledRequestRef = useRef(modelSelectionRequest)

  useEffect(() => {
    if (!modelSelectionRequest || handledRequestRef.current === modelSelectionRequest) {
      return
    }
    handledRequestRef.current = modelSelectionRequest
    wasInputFocusedRef.current = document.activeElement === composerInputRef?.current
    setOpen(true)
  }, [composerInputRef, modelSelectionRequest])

  return { open, setOpen, wasInputFocusedRef }
}
