import { useSyncExternalStore } from 'react'
import {
  getRestartPendingSnapshot,
  subscribeRestart,
} from '../lib/connectivityStore.js'

// Restart state shares the readiness owner; only a ready later boot clears it.
export default function useRestartPending() {
  return useSyncExternalStore(
    subscribeRestart,
    getRestartPendingSnapshot,
    () => false,
  )
}
