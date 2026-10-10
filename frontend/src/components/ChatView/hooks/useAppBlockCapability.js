/* Preserve one startup tap while an app's inline capability is negotiated. */
import { useCallback, useRef, useState } from 'react'

export default function useAppBlockCapability({ allowedKeys, onFallback }) {
  const [supported, setSupported] = useState(null)
  const pending = useRef(null)
  const remember = useCallback(event => {
    if (supported === null) pending.current = event
  }, [supported])
  const observe = useCallback(value => {
    const event = pending.current
    pending.current = null
    setSupported(value)
    if (!value && event?.event === 'activate' && allowedKeys.has(event.key)) onFallback(event.key)
  }, [allowedKeys, onFallback])
  return { supported, remember, observe }
}
