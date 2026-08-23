import { useSyncExternalStore } from 'react'
import {
  getOnlineSnapshot,
  getRecoverySnapshot,
  getReachabilityPhaseSnapshot,
  ReachabilityPhase,
  subscribeOnline,
} from '../lib/connectivityStore.js'

// The shell, retained chats, and app canvases consume one reachability verdict.
// useSyncExternalStore keeps concurrent renders consistent without multiplying
// health probes, browser listeners, intervals, or mobile radio wakeups.
export default function useOnlineStatus() {
  return useSyncExternalStore(subscribeOnline, getOnlineSnapshot, () => true)
}

export function useReachabilityPhase() {
  return useSyncExternalStore(
    subscribeOnline,
    getReachabilityPhaseSnapshot,
    () => ReachabilityPhase.ONLINE,
  )
}

// A monotonic recovery edge for work that failed while the shared transport
// was unavailable. This deliberately shares the same singleton subscriber as
// the phase hooks above; consumers gain an event, not another connection owner.
export function useRecoveryGeneration() {
  return useSyncExternalStore(subscribeOnline, getRecoverySnapshot, () => 0)
}
