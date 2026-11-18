/* Shared clocks: one timer however many components read the time. Wait
   countdowns tick each quarter minute; reply footers each minute. */
import { useSyncExternalStore } from 'react'

// Countdowns read in whole minutes; a 15 s tick keeps them honest without
// re-rendering anything else. Only subscribed components re-render on a tick.
export const COUNTDOWN_TICK_MS = 15_000
export const MINUTE_TICK_MS = 60_000

const noSubscription = () => () => {}

/** A clock that runs while anything subscribes and stops after the last
 *  subscriber leaves. `refreshOnShow` also reads the time at once when a
 *  suspended tab is shown again. */
function sharedClock(tickMs, { refreshOnShow = false } = {}) {
  const listeners = new Set()
  let now = Date.now()
  let timer = null
  const tick = () => {
    now = Date.now()
    listeners.forEach(listener => listener())
  }
  const onVisible = () => { if (document.visibilityState === 'visible') tick() }
  const subscribe = listener => {
    listeners.add(listener)
    // A reader joining reads the time now, not the last tick: the clock may
    // have slept while nothing read it, or ticked up to `tickMs` ago (a reply
    // that just settled must not read as a minute old). Readers re-render only
    // when the time actually moved.
    const time = Date.now()
    if (time !== now) {
      now = time
      listeners.forEach(each => each())
    }
    if (!timer) {
      timer = setInterval(tick, tickMs)
      if (refreshOnShow) document.addEventListener('visibilitychange', onVisible)
    }
    return () => {
      listeners.delete(listener)
      if (!listeners.size && timer) {
        clearInterval(timer)
        timer = null
        if (refreshOnShow) document.removeEventListener('visibilitychange', onVisible)
      }
    }
  }
  const snapshot = () => now
  return {
    use: active => useSyncExternalStore(active ? subscribe : noSubscription, snapshot, snapshot),
    listenerCount: () => listeners.size,
  }
}

const countdown = sharedClock(COUNTDOWN_TICK_MS)
const minute = sharedClock(MINUTE_TICK_MS, { refreshOnShow: true })

/** The shared countdown time; `active` false reads it without ticking. */
export function useCountdownNow(active = true) {
  return countdown.use(active)
}

/** The shared minute time for relative footer times; `active` false reads it
 *  without ticking. */
export function useMinuteNow(active = true) {
  return minute.use(active)
}

export function _countdownListenerCount() {
  return countdown.listenerCount()
}

export function _minuteListenerCount() {
  return minute.listenerCount()
}
