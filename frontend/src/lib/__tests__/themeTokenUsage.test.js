/* Shell styles name theme tokens instead of typing colours: no literal fallbacks behind real tokens, and no token names the theme never defines. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../../', import.meta.url))

// The tokens backend/app/theme.py guarantees on every themed page and app frame.
const TOKENS = [
  'bg', 'surface', 'surface-2', 'border', 'border-light', 'text', 'muted',
  'accent', 'accent-hover', 'accent-dim', 'accent-fg', 'accent-text', 'accent-fill', 'accent-fill-hover',
  'danger', 'danger-text', 'danger-fill', 'green', 'green-text', 'green-fill', 'warning', 'warning-text',
  'font', 'mono',
]
// Names that read like tokens but are never defined, so their fallback always painted.
const UNDEFINED_TOKENS = ['success', 'fg', 'hover', 'text-muted', 'owner-input', 'text-secondary', 'fg-muted']

function sources(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) {
      if (name !== '__tests__' && name !== 'node_modules') sources(path, out)
    } else if (/\.(css|jsx?|mjs)$/.test(name) && !/\.test\./.test(name)) {
      out.push(path)
    }
  }
  return out
}

const COLOUR = String.raw`(?:#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|white\b|black\b)`
const literalFallback = new RegExp(String.raw`var\(\s*--(${TOKENS.join('|')})\s*,\s*${COLOUR}`, 'g')
const undefinedToken = new RegExp(String.raw`var\(\s*--(${UNDEFINED_TOKENS.join('|')})\s*[,)]`, 'g')

test('shell styles use theme tokens without typed colour fallbacks', () => {
  const offenders = []
  for (const file of sources(SRC)) {
    const text = readFileSync(file, 'utf8')
    for (const m of text.matchAll(literalFallback)) offenders.push(`${relative(SRC, file)}: ${m[0]}…`)
    for (const m of text.matchAll(undefinedToken)) offenders.push(`${relative(SRC, file)}: ${m[0]} is not a theme token`)
  }
  assert.deepEqual(offenders, [], 'Use var(--token) alone; every themed page defines it (backend/app/theme.py).')
})
