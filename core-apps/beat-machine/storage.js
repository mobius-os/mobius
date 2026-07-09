import { PADS, PAD_COLORS } from './audio.js'
import { useEffect, useState } from 'react'

export const SAVE_PATH = 'state.json'
const SAVE_VERSION = 2

function storageBridge() {
  return (typeof window !== 'undefined' && window.mobius && window.mobius.storage) || null
}

function headers(token) {
  return { Authorization: `Bearer ${token}` }
}

export async function loadBeatState(appId, token) {
  try {
    const bridge = storageBridge()
    if (bridge && typeof bridge.get === 'function') {
      return sanitizeState(await bridge.get(SAVE_PATH))
    }
    if (!appId || !token) return sanitizeState(null)
    const res = await fetch(`/api/storage/apps/${appId}/${SAVE_PATH}`, { headers: headers(token) })
    if (res.status === 404) return sanitizeState(null)
    if (!res.ok) throw new Error(`GET ${SAVE_PATH} failed (${res.status})`)
    return sanitizeState(await res.json())
  } catch {
    return sanitizeState(null)
  }
}

export async function saveBeatState(appId, token, data) {
  const body = { ...data, version: SAVE_VERSION, updated_at: new Date().toISOString() }
  const bridge = storageBridge()
  if (bridge && typeof bridge.set === 'function') {
    await bridge.set(SAVE_PATH, body)
    return
  }
  if (!appId || !token) return
  const res = await fetch(`/api/storage/apps/${appId}/${SAVE_PATH}`, {
    method: 'PUT',
    headers: { ...headers(token), 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`PUT ${SAVE_PATH} failed (${res.status})`)
}

export function sanitizeState(raw) {
  const state = raw && typeof raw === 'object' ? raw : {}
  return {
    volumes: sanitizeVolumes(state.volumes),
    echo: clamp01(state.echo),
    reverb: clamp01(state.reverb),
    customPads: sanitizeCustomPads(state.customPads),
  }
}

function sanitizeVolumes(value) {
  const input = Array.isArray(value) ? value : []
  return Array.from({ length: PADS }, (_, idx) => {
    const raw = Number(input[idx])
    return Number.isFinite(raw) ? clamp01(raw) : 0.8
  })
}

function sanitizeCustomPads(value) {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => {
      const idx = Number(item?.idx)
      if (!Number.isInteger(idx) || idx < 8 || idx >= PADS) return null
      const audio = item.audio && typeof item.audio === 'object' ? item.audio : null
      if (!audio || !Array.isArray(audio.channels) || audio.channels.length === 0) return null
      return {
        idx,
        name: String(item.name || `Sample ${idx - 7}`).slice(0, 18),
        color: typeof item.color === 'string' ? item.color : PAD_COLORS[idx],
        audio,
      }
    })
    .filter(Boolean)
}

function clamp01(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return 0
  return Math.max(0, Math.min(1, n))
}

export function useOnline() {
  const initial = (() => {
    if (typeof window === 'undefined') return true
    if (typeof window.mobius?.online === 'boolean') return window.mobius.online
    return navigator.onLine !== false
  })()
  const [online, setOnline] = useState(initial)

  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const up = () => setOnline(true)
    const down = () => setOnline(false)
    window.addEventListener('online', up)
    window.addEventListener('offline', down)
    let unsub = null
    if (window.mobius && typeof window.mobius.onChange === 'function') {
      unsub = window.mobius.onChange((state) => {
        if (typeof state?.online === 'boolean') setOnline(state.online)
      })
    }
    return () => {
      window.removeEventListener('online', up)
      window.removeEventListener('offline', down)
      if (unsub) unsub()
    }
  }, [])

  return online
}
