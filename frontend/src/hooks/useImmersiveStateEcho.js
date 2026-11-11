import { useEffect } from 'react'

/**
 * Re-sends the shell's applied immersive verdict to an app's frames.
 *
 * The runtime's window.mobius.immersive.set() is optimistic: the app's
 * `.hidden` flips as soon as it asks. So the echo must run not only when the
 * verdict flips (`immersiveMode`) or a rebuilt frame is promoted
 * (`liveVersion`), but after EVERY request (`requestCount`): a request the
 * shell holds, such as one from a pane sharing the screen, leaves the verdict
 * unchanged, and only this echo tells the app it did not take effect.
 *
 * `echo` sends the current verdict to each loaded frame; the latest one is
 * used, so it may close over the render's values.
 */
export default function useImmersiveStateEcho({ immersiveMode, liveVersion, requestCount }, echo) {
  useEffect(() => {
    echo()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [immersiveMode, liveVersion, requestCount])
}
