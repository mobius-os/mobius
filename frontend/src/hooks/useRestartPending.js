import { useSyncExternalStore } from 'react'
import {
  getRestartPendingSnapshot,
  subscribeRestart,
} from '../lib/restartStore.js'

// Whether Möbius is mid-restart. Backed by the module-singleton restartStore so
// the shell dot and the queued-message copy read one verdict without racing.
export default function useRestartPending() {
  return useSyncExternalStore(
    subscribeRestart,
    getRestartPendingSnapshot,
    () => false,
  )
}
