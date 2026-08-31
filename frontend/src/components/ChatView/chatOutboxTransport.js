import { apiFetch } from '../../api/client.js'
import { outboxRequestPath } from './chatOutbox.js'

// Network ownership for chatOutbox stays in this tiny adapter so the durable
// state machine remains directly testable without Vite's build-time client
// environment. apiFetch supplies the current owner/embed credential and owns
// expired-session cleanup; the outbox supplies the bounded AbortSignal.
export function requestOutboxDelivery(record, { signal } = {}) {
  return apiFetch(
    outboxRequestPath(record.chatId),
    {
      method: 'POST',
      body: JSON.stringify(record.body),
      signal,
    },
  )
}
