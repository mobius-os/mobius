import { useEffect, useRef } from 'react'
import { clearProviderSwitch } from '../providerSwitch.js'

// Dismissal collapses artifacts and discards only an unconfirmed switch.
// Opening, mounting closed, and in-flight or committed switches are untouched.
export default function useComposerPopoverClose(
  open, providerSwitchStatus, chatId, setArtifactsExpanded,
) {
  const wasOpenRef = useRef(open)
  useEffect(() => {
    const closed = wasOpenRef.current && !open
    wasOpenRef.current = open
    if (closed) {
      setArtifactsExpanded(false)
      if (providerSwitchStatus === 'confirming') clearProviderSwitch(chatId)
    }
  }, [open, providerSwitchStatus, chatId, setArtifactsExpanded])
}
