import { useEffect } from 'react'
import { registerNativePush } from '../lib/nativePush.js'

/** On shell start in the Möbius app, connects this phone for native push, or forgets it if notifications are not allowed. */
export default function useNativePush() {
  useEffect(() => {
    registerNativePush().catch(() => {})
  }, [])
}
