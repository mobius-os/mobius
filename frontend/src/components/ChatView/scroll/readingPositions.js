/**
 * Durable reading-position storage for chat scroll.
 *
 * This module owns only serialization and bounded retention. It deliberately
 * knows nothing about DOM geometry or live scroll modes: the controller turns
 * its current state into a durable anchor before calling `writeReadingPosition`.
 */

export const READING_POSITION_KEY = 'chat-reading-position'
const READING_POSITION_LIMIT = 300

const positions = (() => {
  try {
    const parsed = JSON.parse(localStorage.getItem(READING_POSITION_KEY) || '{}')
    return (parsed && typeof parsed === 'object') ? parsed : {}
  }
  catch { return {} }
})()

// Logout is a terminal owner-session boundary. React/page lifecycle callbacks
// may still run before reload; disabling writes prevents those late callbacks
// from recreating data that logout just removed.
let writesEnabled = true

function persist() {
  if (!writesEnabled) return
  try {
    const entries = Object.entries(positions)
    if (entries.length > READING_POSITION_LIMIT) {
      const expired = entries
        .sort((a, b) => (b[1]?.at || 0) - (a[1]?.at || 0))
        .slice(READING_POSITION_LIMIT)
      for (const [chatId] of expired) delete positions[chatId]
    }
    localStorage.setItem(READING_POSITION_KEY, JSON.stringify(positions))
  }
  catch { /* best-effort position storage must never break scrolling */ }
}

export function readingPositionFor(chatId) {
  return positions[String(chatId || '')] || null
}

export function hasReadingPosition(chatId) {
  return Object.hasOwn(positions, String(chatId || ''))
}

export function writeReadingPosition(chatId, mode) {
  const id = String(chatId || '')
  if (!id || !mode || mode.kind === 'INITIAL') {
    if (id) delete positions[id]
  } else {
    positions[id] = { ...mode, at: Date.now() }
  }
  persist()
}

export function forgetReadingPosition(chatId) {
  const id = String(chatId || '')
  if (!(id in positions)) return false
  delete positions[id]
  persist()
  return true
}

/** The durable message row an activation needs before reveal. */
export function savedReadingAnchorKey(chatId) {
  const mode = readingPositionFor(chatId)
  return mode?.kind === 'ANCHOR_AT' && typeof mode.key === 'string'
    ? mode.key
    : null
}

/** Nested part paths need committed DOM validation before cache reveal. */
export function savedReadingAnchorHasNestedPart(chatId) {
  const mode = readingPositionFor(chatId)
  return mode?.kind === 'ANCHOR_AT'
    && Array.isArray(mode.part)
    && mode.part.length > 0
}

/** Replace one saved alias before restore consumes it. */
export function remapSavedReadingAnchor(chatId, fromKey, toKey) {
  const id = String(chatId || '')
  const mode = positions[id]
  if (mode?.kind !== 'ANCHOR_AT'
      || mode.key !== fromKey
      || typeof toKey !== 'string'
      || !toKey) return false
  positions[id] = { ...mode, key: toKey, at: Date.now() }
  persist()
  return true
}

export const retireSavedReadingPosition = forgetReadingPosition

export function clearReadingPositions() {
  writesEnabled = false
  for (const key of Object.keys(positions)) delete positions[key]
  try { localStorage.removeItem(READING_POSITION_KEY) } catch {}
}
