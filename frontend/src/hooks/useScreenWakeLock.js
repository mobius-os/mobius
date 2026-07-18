import { useEffect } from 'react'
import { createScreenWakeLockController } from '../lib/screenWakeLock.js'

/** Keep the display awake for the lifetime of a visible, active interaction. */
export default function useScreenWakeLock(active) {
  useEffect(() => {
    if (!active) return undefined
    const controller = createScreenWakeLockController()
    controller.start()
    return () => controller.stop()
  }, [active])
}
