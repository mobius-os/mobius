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
  const canvas = read('src/components/AppCanvas/AppCanvas.jsx')
  assert.match(appRoot, /readStandaloneBoot\(\)/)
  assert.match(appRoot, /<StandaloneApp initialApp=\{STANDALONE_APP\}/)
  assert.match(standalone, /<AppCanvas/)
  assert.match(canvas, /useImperativeHandle\(hostRef/)
  assert.doesNotMatch(standalone, /querySelector\(['"]iframe/,
    'history commands must stay inside the exact live-frame owner')
  assert.doesNotMatch(standalone, /\/api\/apps\/.*\/module/)
  assert.doesNotMatch(standalone, /localStorage\.getItem\(['"]token/)
})

test('standalone chat navigation delegates draft and autosend ownership', () => {
  const standalone = read('src/components/StandaloneApp/StandaloneApp.jsx')
  assert.match(standalone, /stageComposerHandoff\(request\.chatId, request\.draft\)/)
  assert.match(standalone,
    /stageComposerHandoff\(chat\.id, request\.draft, \{ autoSend: request\.autoSend \}\)/)
  assert.doesNotMatch(standalone, /sessionStorage\.(?:setItem|removeItem)/)
})

test('generic and standalone crashes share one recovery panel contract', () => {
  const standalone = read('src/components/StandaloneApp/StandaloneApp.jsx')
  const boundary = read('src/components/ErrorBoundary/ErrorBoundary.jsx')
  const panel = read('src/components/ErrorBoundary/RecoveryPanel.jsx')

  assert.match(standalone, /<RecoveryPanel/)
  assert.match(boundary, /<RecoveryPanel/)
  assert.match(panel, /Refreshing didn’t fix/)
  assert.match(panel, /Repair chat request failed/)
  assert.doesNotMatch(standalone, /agentError|Repair chat request failed/)
  assert.doesNotMatch(boundary, /agentError|Repair chat request failed/)
})
