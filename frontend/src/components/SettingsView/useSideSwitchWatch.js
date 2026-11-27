import { useEffect, useState } from 'react'
import { api, jsonOrThrow } from '../../api/client.js'

export const RECHECK_MS = 3000
export const GIVE_UP_AFTER = 60

/**
 * After a launcher switch the restart drops the connection. Reloads the page once the
 * server is back on `target`; returns true once the wait has been given up, after which
 * nothing keeps asking.
 */
export function useSideSwitchWatch(target) {
  const [stuck, setStuck] = useState(false)

  useEffect(() => {
    if (!target) return undefined
    let checks = 0
    const timer = setInterval(() => {
      checks += 1
      if (checks > GIVE_UP_AFTER) {
        setStuck(true)
        clearInterval(timer)
        return
      }
      api.platform.launcherSide().then(jsonOrThrow)
        .then(value => { if (value.side === target) window.location.reload() })
        .catch(() => {})
    }, RECHECK_MS)
    return () => clearInterval(timer)
  }, [target])

  return stuck
}
