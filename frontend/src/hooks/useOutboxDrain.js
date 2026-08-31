import { useEffect } from 'react'
import { getToken } from '../api/client.js'
import {
  deliverIntent,
  drainOutbox,
  outboxPrincipalKey,
} from '../components/ChatView/chatOutbox.js'
import { requestOutboxDelivery } from '../components/ChatView/chatOutboxTransport.js'
import {
  getRecoverySnapshot,
  subscribeRecovery,
} from '../lib/connectivityStore.js'

// Replays the durable chat outbox whenever connectivity is (re)established or the
// tab returns to the foreground. Mounted once at the shell so a queued send or
// answer reaches the server as soon as the shell reconnects, regardless of which
// view is open (and even from the empty new-chat landing). drainOutbox is
// single-flight, so this + a mounted chat's own reconnect can't double-POST.
export default function useOutboxDrain() {
  useEffect(() => {
    const drain = () => drainOutbox({
      deliver: record => deliverIntent(record, requestOutboxDelivery),
      principalKey: outboxPrincipalKey(getToken()),
    })
    let observedRecoveryGeneration = getRecoverySnapshot()
    void drain() // sweep anything left by a prior session on mount
    const unsubscribeRecovery = subscribeRecovery(() => {
      const generation = getRecoverySnapshot()
      if (generation === observedRecoveryGeneration) return
      observedRecoveryGeneration = generation
      void drain()
    })
    const onWake = () => {
      if (document.visibilityState === 'visible') void drain()
    }
    window.addEventListener('online', onWake)
    window.addEventListener('focus', onWake)
    document.addEventListener('visibilitychange', onWake)
    return () => {
      unsubscribeRecovery()
      window.removeEventListener('online', onWake)
      window.removeEventListener('focus', onWake)
      document.removeEventListener('visibilitychange', onWake)
    }
  }, [])
}
