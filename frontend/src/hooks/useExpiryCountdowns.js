import { useEffect, useState } from 'react'
import { expiryCountdown } from '../components/Drawer/uninstalledApps.js'

/**
 * The rows that can still be recovered, each with its countdown.
 *
 * Each row's `seconds_left` was measured by the server when the list was sent,
 * so it is counted down from `receivedAt`, the moment this device got the list,
 * rather than compared with `expires_at`. A device clock that is off by hours
 * then cannot hide an app the server would still recover. The row can only
 * linger past the server's deadline by the time the response took to arrive,
 * and a recover in that gap gets the server's "expired" answer.
 *
 * The view wakes once, when the soonest label changes, and times that wake
 * against the real clock, so a deadline that has already passed is caught up
 * at once.
 */
export default function useExpiryCountdowns(rows, receivedAt) {
  const [now, setNow] = useState(() => Date.now())
  // `now` only moves when the timer fires, so rows that arrive between wakes
  // are measured against an older moment. That is deliberate and safe: a
  // stale label differs from the true one only when some `changesAt` has
  // already passed, and then the timer below fires at once. Reading Date.now()
  // during render would make render impure without changing the outcome.
  const live = rows
    .map(row => ({ row, countdown: expiryCountdown(receivedAt + row.seconds_left * 1000, now) }))
    .filter(entry => entry.countdown)
  const nextChangeAt = Math.min(...live.map(entry => entry.countdown.changesAt))

  useEffect(() => {
    if (!Number.isFinite(nextChangeAt)) return undefined
    const timer = setTimeout(() => setNow(Date.now()), Math.max(0, nextChangeAt - Date.now()))
    return () => clearTimeout(timer)
  }, [now, nextChangeAt])

  return live
}
