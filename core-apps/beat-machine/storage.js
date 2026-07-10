import { CUSTOM_START, PADS, PAD_COLORS, TOTAL_BEATS } from './audio.js'
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
  const bridge = storageBridge()
  if (bridge && typeof bridge.get === 'function') {
    return sanitizeState(await bridge.get(SAVE_PATH))
  }
  if (!appId || !token) return sanitizeState(null)
  const res = await fetch(`/api/storage/apps/${appId}/${SAVE_PATH}`, { headers: headers(token) })
  if (res.status === 404) return sanitizeState(null)
  if (!res.ok) throw new Error(`GET ${SAVE_PATH} failed (${res.status})`)
  return sanitizeState(await res.json())
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
  const state = unwrapStorageEnvelope(raw)
  return {
    grid: sanitizeGrid(state.grid),
    bpm: sanitizeBpm(state.bpm),
    volumes: sanitizeVolumes(state.volumes),
    echo: clamp01(state.echo),
    reverb: clamp01(state.reverb),
    customPads: sanitizeCustomPads(state.customPads),
  }
}

export function createEmptyGrid() {
  return Array.from({ length: PADS }, () => new Array(TOTAL_BEATS).fill(false))
}

function unwrapStorageEnvelope(raw) {
  if (raw && typeof raw === 'object' && typeof raw.content === 'string') {
    try {
      const parsed = JSON.parse(raw.content)
      return parsed && typeof parsed === 'object' ? parsed : {}
    } catch {
      return {}
    }
  }
  return raw && typeof raw === 'object' ? raw : {}
}

function sanitizeGrid(value) {
  const rows = Array.isArray(value) ? value : []
  return Array.from({ length: PADS }, (_, padIdx) => {
    const row = Array.isArray(rows[padIdx]) ? rows[padIdx] : []
    return Array.from({ length: TOTAL_BEATS }, (_, beatIdx) => row[beatIdx] === true)
  })
}

function sanitizeBpm(value) {
  const bpm = Number(value)
  if (!Number.isFinite(bpm)) return 120
  return Math.max(60, Math.min(200, Math.round(bpm)))
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
      if (!Number.isInteger(idx) || idx < CUSTOM_START || idx >= PADS) return null
      const audio = item.audio && typeof item.audio === 'object' ? item.audio : null
      if (!audio || !Array.isArray(audio.channels) || audio.channels.length === 0) return null
      return {
        idx,
        name: String(item.name || `Rec ${idx - CUSTOM_START + 1}`).slice(0, 18),
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
    if (window.mobius && typeof window.mobius.onOnlineChange === 'function') {
      unsub = window.mobius.onOnlineChange((next) => {
        setOnline(next !== false)
      })
    } else if (window.mobius && typeof window.mobius.onChange === 'function') {
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
