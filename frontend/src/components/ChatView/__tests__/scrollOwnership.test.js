import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'

// Sole-ownership guard for the dynamic bottom spacer — the resource the chat
// scroll contract fights over. useScrollMode.js's sizeSpacer and
// preserveTogglePosition.js's collapse-prime are the ONLY sanctioned writers of
// `.spacer-dynamic` height; they coordinate the pin/anchor geometry. A third
// component writing that height directly is exactly how the collapse-bounce and
// spacer-over-reservation regressions entered — it mutates the contested
// quantity without going through (or informing) the mode machine. This
// source-scan makes a new writer trip a red test instead of shipping a scroll
// bug. (When preserveTogglePosition is folded into useScrollMode — ARCHITECTURE
// "one owner" hardening — drop it from ALLOWED and this proves single ownership.)

const dir = dirname(fileURLToPath(import.meta.url))
const chatViewDir = join(dir, '..')

const ALLOWED = new Set(['useScrollMode.js', 'preserveTogglePosition.js'])

// A line that assigns a height to the dynamic spacer. Matches
// `spacer.style.height = ...` / `spacerEl.style.height = ...` on any variable,
// scoped to files that reference the spacer selector so a stray `.style.height`
// on an unrelated element (e.g. the composer textarea) is not a false hit.
const SPACER_HEIGHT_WRITE = /\.style\.height\s*=/

function sourceFiles(root) {
  const out = []
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    if (entry.name === '__tests__' || entry.name === 'node_modules') continue
    const full = join(root, entry.name)
    if (entry.isDirectory()) out.push(...sourceFiles(full))
    else if (/\.(js|jsx)$/.test(entry.name)) out.push({ name: entry.name, full })
  }
  return out
}

test('only sanctioned modules write the dynamic spacer height', () => {
  const offenders = []
  for (const { name, full } of sourceFiles(chatViewDir)) {
    if (ALLOWED.has(name)) continue
    const src = readFileSync(full, 'utf8')
    if (!src.includes('.spacer-dynamic')) continue
    if (SPACER_HEIGHT_WRITE.test(src)) offenders.push(name)
  }
  assert.deepEqual(offenders, [],
    `these modules write .spacer-dynamic height outside the sanctioned owners `
    + `(${[...ALLOWED].join(', ')}): ${offenders.join(', ')}. Route spacer sizing `
    + `through useScrollMode's sizeSpacer instead of mutating it directly.`)
})

test('the two sanctioned spacer owners still exist (guard is not vacuous)', () => {
  // If a refactor renames these files the guard above would silently pass with
  // nothing to check; assert the owners are present so the guard stays live.
  const writers = sourceFiles(chatViewDir).filter(({ full }) => {
    const src = readFileSync(full, 'utf8')
    return src.includes('.spacer-dynamic') && SPACER_HEIGHT_WRITE.test(src)
  }).map(f => f.name).sort()
  assert.deepEqual(writers, ['preserveTogglePosition.js', 'useScrollMode.js'])
})
