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

test('standalone navigation delegates chat ownership and shares crash recovery', () => {
  const standalone = read('src/components/StandaloneApp/StandaloneApp.jsx')
  assert.match(standalone, /stageComposerHandoff\(request\.chatId, request\.draft\)/)
  assert.match(standalone,
    /stageComposerHandoff\(chat\.id, request\.draft, \{ autoSend: request\.autoSend \}\)/)
  assert.doesNotMatch(standalone, /sessionStorage\.(?:setItem|removeItem)/)

  const boundary = read('src/components/ErrorBoundary/ErrorBoundary.jsx')

  assert.match(standalone, /<RecoveryPanel/)
  assert.match(boundary, /<RecoveryPanel/)
})

test('workspace and standalone hosts share the Projects controller and response bridge', () => {
  const shell = read('src/components/Shell/Shell.jsx')
  const standalone = read('src/components/StandaloneApp/StandaloneApp.jsx')
  const canvas = read('src/components/AppCanvas/AppCanvas.jsx')
  const runtime = read('src/runtime/index.js')

  assert.match(shell, /handleAppProjectsRequest\(\{/)
  assert.match(standalone, /handleAppProjectsRequest\(\{/)
  assert.match(canvas, /'moebius:projects-result'/)
  assert.match(runtime, /projects: makeProjects\(\)|const projects = makeProjects\(\)/)
})
