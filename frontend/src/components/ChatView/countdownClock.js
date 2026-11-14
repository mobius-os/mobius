/* One shared quarter-minute clock for wait countdowns: one timer however many cards show a countdown. */
import { useSyncExternalStore } from 'react'

// Countdowns read in whole minutes; a 15 s tick keeps them honest without
// re-rendering anything else. Only subscribed components re-render on a tick.
export const COUNTDOWN_TICK_MS = 15_000

const listeners = new Set()
let now = Date.now()
let timer = null

function tick() {
  now = Date.now()
  listeners.forEach(listener => listener())
}

function subscribe(listener) {
  listeners.add(listener)
  if (!timer) {
    // The clock slept while nothing showed a countdown; catch up at once.
    now = Date.now()
    timer = setInterval(tick, COUNTDOWN_TICK_MS)
  }
  return () => {
    listeners.delete(listener)
    if (!listeners.size && timer) {
      clearInterval(timer)
      timer = null
    }
  }
}

const noSubscription = () => () => {}
const snapshot = () => now

/** The shared countdown time; `active` false reads it without ticking. */
export function useCountdownNow(active = true) {
  return useSyncExternalStore(active ? subscribe : noSubscription, snapshot, snapshot)
}

export function _countdownListenerCount() {
  return listeners.size
}
