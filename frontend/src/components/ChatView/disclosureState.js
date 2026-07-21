import { useRef, useState } from 'react'


// Disclosure state is screen state, not transcript data. Keep it for the
// browser session so leaving a chat and returning restores the exact activity,
// thought, and tool rows the reader opened without writing presentation state
// into the durable conversation.
const STORAGE_PREFIX = 'chat-disclosures:'
const MAX_OPEN_DISCLOSURES = 200
const cache = new Map()

function storageKey(chatId) {
  return `${STORAGE_PREFIX}${chatId}`
}

function readOpenKeys(chatId) {
  const id = String(chatId || '')
  if (!id) return new Set()
  if (cache.has(id)) return cache.get(id)
  let keys = []
  try {
    const parsed = JSON.parse(sessionStorage.getItem(storageKey(id)) || '[]')
    if (Array.isArray(parsed)) keys = parsed.filter(key => typeof key === 'string')
  } catch {}
  const openKeys = new Set(keys.slice(-MAX_OPEN_DISCLOSURES))
  cache.set(id, openKeys)
  return openKeys
}

export function persistDisclosureOpen(chatId, disclosureKey, open) {
  const id = String(chatId || '')
  const key = String(disclosureKey || '')
  if (!id || !key) return
  const openKeys = readOpenKeys(id)
  // Refresh insertion order when opening so the bounded set retains the most
  // recently used disclosures in an unusually long tool-heavy chat.
  openKeys.delete(key)
  if (open) openKeys.add(key)
  while (openKeys.size > MAX_OPEN_DISCLOSURES) {
    openKeys.delete(openKeys.values().next().value)
  }
  try {
    sessionStorage.setItem(storageKey(id), JSON.stringify([...openKeys]))
  } catch {}
}

export function disclosureIsOpen(chatId, disclosureKey) {
  return readOpenKeys(chatId).has(String(disclosureKey || ''))
}

export function useDisclosureState(chatId, disclosureKey) {
  const [open, setOpenState] = useState(
    () => disclosureIsOpen(chatId, disclosureKey),
  )
  const openRef = useRef(open)
  openRef.current = open
  const setOpen = (next) => {
    const value = typeof next === 'function' ? !!next(openRef.current) : !!next
    openRef.current = value
    persistDisclosureOpen(chatId, disclosureKey, value)
    setOpenState(value)
  }
  return [open, setOpen]
}

export function _resetDisclosureStateForTests() {
  cache.clear()
}
