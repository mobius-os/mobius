import { useEffect } from 'react'
import { getToken } from '../api/client.js'
import {
  deliverIntent,
  drainOutbox,
  outboxPrincipalKey,
  subscribeOutboxChanges,
} from '../components/ChatView/chatOutbox.js'
import { requestOutboxDelivery } from '../components/ChatView/chatOutboxTransport.js'
import {
  getDeliveryReadySnapshot,
  subscribeDeliveryReady,
} from '../lib/connectivityStore.js'

// The shared readiness owner already observes wake, transport, and restart.
// Replay on its ready edge, not raw browser events that may precede the server.
export default function useOutboxDrain() {
  useEffect(() => {
    let disposed = false
    let draining = false
    let requested = false
    const drain = async () => {
      if (disposed || !getDeliveryReadySnapshot()) return
      requested = true
      if (draining) return
      draining = true
      try {
        // An enqueue can arrive after the active drain read its snapshot.
        // Coalesce that explicit request into one next pass, not a retry loop.
        while (requested && !disposed && getDeliveryReadySnapshot()) {
          requested = false
          await drainOutbox({
            deliver: record => getDeliveryReadySnapshot()
              ? deliverIntent(record, requestOutboxDelivery)
              : 'retry',
            principalKey: outboxPrincipalKey(getToken()),
          })
        }
      } finally {
        draining = false
      }
    }
    let wasReady = getDeliveryReadySnapshot()
    const unsubscribe = subscribeDeliveryReady(() => {
      const ready = getDeliveryReadySnapshot()
      if (ready && !wasReady) void drain()
      wasReady = ready
    })
    const unsubscribeChanges = subscribeOutboxChanges(change => {
      // Both interactive and replayed answers can release older follow-ups.
      // Coalesce that acknowledgement through this same drain owner.
      const answerAccepted = change.kind === 'retire'
        && change.outcome === 'delivered' && change.record?.type === 'answer'
      if (change.requestDelivery || answerAccepted) void drain()
    })
    void drain()
    return () => {
      disposed = true
      unsubscribe()
      unsubscribeChanges()
    }
  }, [])
}
