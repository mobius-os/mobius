import { useSyncExternalStore } from 'react'
import {
  getOnlineSnapshot,
  subscribeOnline,
} from '../lib/connectivityStore.js'

// The shell, retained chats, and app canvases consume one reachability verdict.
// useSyncExternalStore keeps concurrent renders consistent without multiplying
// health probes, browser listeners, intervals, or mobile radio wakeups.
export default function useOnlineStatus() {
  return useSyncExternalStore(subscribeOnline, getOnlineSnapshot, () => true)
}
