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
  assert.match(standalone, /openChatWithAppDraft\(request\.chatId, request\.draft, \{ open, openNew: openNewChat \}\)/,
    'an app draft for an existing chat is additive, never a replacement')
  assert.match(standalone, /stageComposerHandoff\(chat\.id, draft\)/)
  assert.doesNotMatch(standalone, /autoSend/,
    'an app new-chat request stages an editable draft, never a submitted turn')
  assert.doesNotMatch(standalone, /sessionStorage\.(?:setItem|removeItem)/)

  const boundary = read('src/components/ErrorBoundary/ErrorBoundary.jsx')

  assert.match(standalone, /<RecoveryPanel/)
  assert.match(boundary, /<RecoveryPanel/)
})

test('only the owner\'s own /new command can auto-send a new workspace chat', () => {
  const shell = read('src/components/Shell/Shell.jsx')
  const newChat = shell.match(/async function newChat\(\{([^}]*)\}[^]*?\n  \}\n/)
  assert.ok(newChat, 'Shell defines newChat')
  assert.match(newChat[1], /\bautoSend\b/, '/new <message> passes autoSend to newChat')
  assert.match(newChat[0], /stageComposerHandoff\(chatId, suppliedDraft, \{\s*autoSend: !!autoSend && settingsCopied,?\s*\}\)/)

  const appRequest = shell.match(/request\.type === 'moebius:new-chat'\) \{[^]*?\n      \}\n/)
  assert.ok(appRequest, 'Shell handles an app new-chat request')
  assert.doesNotMatch(appRequest[0], /autoSend/,
    'an app new-chat request stages an editable draft, never a submitted turn')
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
