import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const frontend = resolve(here, '../../..')
const read = path => readFileSync(resolve(frontend, path), 'utf8')

test('standalone route selects the shared opaque AppCanvas host', () => {
  const appRoot = read('src/App.jsx')
  const standalone = read('src/components/StandaloneApp/StandaloneApp.jsx')
  assert.match(appRoot, /readStandaloneBoot\(\)/)
  assert.match(appRoot, /<StandaloneApp initialApp=\{STANDALONE_APP\}/)
  assert.match(standalone, /<AppCanvas/)
  assert.doesNotMatch(standalone, /\/api\/apps\/.*\/module/)
  assert.doesNotMatch(standalone, /localStorage\.getItem\(['"]token/)
})

test('standalone approval handoffs preserve exact one-shot autosend text', () => {
  const standalone = read('src/components/StandaloneApp/StandaloneApp.jsx')
  assert.match(standalone, /setItem\('pending-draft-autosend', draft\)/)
  assert.match(standalone, /setItem\(`draft-autosend:\$\{chatId\}`, draft\)/)
  assert.doesNotMatch(standalone, /setItem\('pending-draft-autosend', '1'\)/)
})
