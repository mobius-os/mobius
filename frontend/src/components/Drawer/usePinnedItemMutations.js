/* The one owner of pin and unpin for chats, apps, and projects. Shell holds it
   so the drawer and a project page's Chats list pin through the same queue,
   pending state, and recovery; the drawer's pinned reorder joins the same
   queue so every pinned-list change commits in owner-intent order. */

import { useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, reservePinnedIntent } from '../../api/client.js'
import { appQueries, chatQueries, projectQueries } from '../../hooks/queries.js'
import { createPinnedMutationQueue } from './pinnedMutationQueue.js'
import {
  nextPendingPinnedAt,
  projectPendingDrawerPins,
} from './drawerInformationArchitecture.js'

const PIN_TARGETS = {
  chat: {
    queryKey: chatQueries.keys.all,
    request: (id, pinned, witness) => api.chats.setPinned(id, pinned, { intentWitness: witness }),
    recover: queryClient => chatQueries.list.invalidate(queryClient),
  },
  app: {
    queryKey: appQueries.keys.all,
    request: (id, pinned, witness) => api.apps.setPinned(id, pinned, { intentWitness: witness }),
    recover: queryClient => appQueries.list.invalidate(queryClient),
  },
  project: {
    queryKey: projectQueries.keys.all,
    request: (id, pinned, witness) => api.projects.setPinned(id, pinned, { intentWitness: witness }),
    recover: queryClient => projectQueries.list.invalidate(queryClient),
  },
}

/**
 * `projectedItems` is `{ chats, apps, projects }` with every pin still in
 * flight already applied, so each surface shows the owner's choice at once.
 * `pin(kind, id, pinned)` resolves once that change has settled. `enqueue`
 * runs another pinned-list mutation in the same order.
 */
export default function usePinnedItemMutations({ chats, apps, projects, onNotice }) {
  const queryClient = useQueryClient()
  const generationRef = useRef(0)
  const queueRef = useRef(null)
  if (queueRef.current === null) queueRef.current = createPinnedMutationQueue()
  const lastPendingPinnedAtRef = useRef(null)
  const [pendingPins, setPendingPins] = useState(() => new Map())
  const projectedItems = useMemo(() => projectPendingDrawerPins(
    chats,
    apps,
    projects,
    pendingPins,
  ), [chats, apps, projects, pendingPins])

  function pin(kind, id, pinned) {
    const { queryKey, request, recover } = PIN_TARGETS[kind]
    const generation = ++generationRef.current
    const intentWitness = reservePinnedIntent()
    const itemKey = `${kind}:${id}`
    const optimisticPinnedAt = pinned
      ? nextPendingPinnedAt(
        projectedItems.chats,
        projectedItems.apps,
        projectedItems.projects,
        { previous: lastPendingPinnedAtRef.current },
      )
      : null
    if (optimisticPinnedAt) lastPendingPinnedAtRef.current = optimisticPinnedAt
    const pending = {
      generation,
      pinned,
      pinnedAt: optimisticPinnedAt,
    }
    setPendingPins(current => new Map(current).set(itemKey, pending))

    return queueRef.current.enqueue(async () => {
      try {
        const updated = await request(id, pinned, await intentWitness)
        const persistedPinnedAt = updated?.pinned_at ?? null
        if (pinned && !persistedPinnedAt) {
          throw new Error('Pin update returned no canonical rank')
        }

        // Retire any list request that began before the committed write, then
        // place the server's exact rank in the canonical cache. The pending
        // projection keeps the owner's choice visible until this queue item
        // retires, but never becomes durable ordering truth.
        try {
          await queryClient.cancelQueries({ queryKey, exact: true })
        } catch {}
        const applyPersistedPin = list => (list || []).map(item => (
          String(item.id) === String(id)
            ? { ...item, pinned_at: persistedPinnedAt }
            : item
        ))
        queryClient.setQueryData(queryKey, applyPersistedPin)
      } catch {
        onNotice?.(`Couldn’t ${pinned ? 'pin' : 'unpin'} that item.`, { variant: 'error' })
        // A timeout can race a successful server commit. Recovery is outside
        // the queue so one unavailable list read cannot block later intent.
        void Promise.resolve().then(() => recover(queryClient)).catch(() => {})
      } finally {
        setPendingPins(current => {
          if (current.get(itemKey)?.generation !== generation) return current
          const next = new Map(current)
          next.delete(itemKey)
          return next
        })
      }
    })
  }

  return {
    projectedItems,
    pin,
    enqueue: mutation => queueRef.current.enqueue(mutation),
  }
}
