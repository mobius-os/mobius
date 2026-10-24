/* A small per-app cap on idle hidden session frames. Each passive block near
   the viewport asks for a slot; the nearest PASSIVE_IDLE_CAP idle blocks of an
   app get one and the rest stay static (explicit open). A retained session,
   which owns an unacknowledged event or an unresolved outcome, always keeps
   its frame and does not use an idle slot. */
import { useEffect, useState, useSyncExternalStore } from 'react'

export const PASSIVE_IDLE_CAP = 3

/** The ids granted a frame among one app's entries. */
export function grantedSlots(entries, cap = PASSIVE_IDLE_CAP) {
  const granted = new Set(entries.filter(entry => entry.retained).map(entry => entry.id))
  entries.filter(entry => entry.wanted && !entry.retained)
    .sort((a, b) => a.distance - b.distance).slice(0, cap)
    .forEach(entry => granted.add(entry.id))
  return granted
}

export function createPassiveSlots(cap = PASSIVE_IDLE_CAP) {
  const apps = new Map()
  const granted = new Map()
  const listeners = new Set()
  function recompute(appId) {
    const entries = apps.get(appId)
    granted.set(appId, grantedSlots([...(entries?.values() || [])], cap))
    if (!entries?.size) { apps.delete(appId); granted.delete(appId) }
    listeners.forEach(listener => listener())
  }
  return {
    update(appId, id, entry) {
      if (!apps.has(appId)) apps.set(appId, new Map())
      apps.get(appId).set(id, { id, ...entry })
      recompute(appId)
    },
    remove(appId, id) {
      apps.get(appId)?.delete(id)
      recompute(appId)
    },
    granted(appId, id) { return granted.get(appId)?.has(id) === true },
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
}

export const passiveSlots = createPassiveSlots()

export function usePassiveSlot(appId, { wanted, retained, distance }, slots = passiveSlots) {
  const [id] = useState(() => ({}))
  useEffect(() => {
    slots.update(appId, id, { wanted, retained, distance })
  }, [slots, appId, id, wanted, retained, distance])
  useEffect(() => () => slots.remove(appId, id), [slots, appId, id])
  return useSyncExternalStore(slots.subscribe, () => slots.granted(appId, id), () => false)
}
