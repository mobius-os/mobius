/* Delays transient connection notices without delaying delivery safety or terminal errors. */
import { useEffect, useState } from 'react'

export const CONNECTION_NOTICE_DELAY_MS = 2500

export default function useDelayedConnectionNotice(active) {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    if (!active) {
      setVisible(false)
      return undefined
    }
    const timer = setTimeout(() => setVisible(true), CONNECTION_NOTICE_DELAY_MS)
    return () => clearTimeout(timer)
  }, [active])

  return active && visible
}
