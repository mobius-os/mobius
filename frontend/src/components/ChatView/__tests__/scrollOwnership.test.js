import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'

// Sole-writer guard for the dynamic bottom spacer. useScrollMode derives its
// exact height from the latest user row's visibility and content deficit. A
// disclosure, renderer, or component writing the same height independently can
// strand provisional blank room after QA/tool/image layout changes, so any
// second writer is a contract bug.

const dir = dirname(fileURLToPath(import.meta.url))
const chatViewDir = join(dir, '..')

const OWNER = 'useScrollMode.js'

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
    if (name === OWNER) continue
    const src = readFileSync(full, 'utf8')
    if (!src.includes('.spacer-dynamic')) continue
    if (SPACER_HEIGHT_WRITE.test(src)) offenders.push(name)
  }
  assert.deepEqual(offenders, [],
    `these modules write .spacer-dynamic height outside its sole owner `
    + `(${OWNER}): ${offenders.join(', ')}. Route spacer sizing `
    + `through useScrollMode's sizeSpacer instead of mutating it directly.`)
})

test('the sole spacer owner still exists (guard is not vacuous)', () => {
  // If a refactor renames these files the guard above would silently pass with
  // nothing to check; assert the owners are present so the guard stays live.
  const writers = sourceFiles(chatViewDir).filter(({ full }) => {
    const src = readFileSync(full, 'utf8')
    return src.includes('.spacer-dynamic') && SPACER_HEIGHT_WRITE.test(src)
  }).map(f => f.name).sort()
  assert.deepEqual(writers, [OWNER])
})
