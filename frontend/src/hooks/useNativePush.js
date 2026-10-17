import { useEffect } from 'react'
import { registerNativePush } from '../lib/nativePush.js'

/** On shell start, re-registers this phone for native push if the owner turned it on in Settings. */
export default function useNativePush() {
  useEffect(() => {
    registerNativePush().catch(() => {})
  }, [])
}
