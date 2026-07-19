import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
const shellCss = readFileSync(new URL('../Shell.css', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../../ChatView/ChatView.jsx', import.meta.url), 'utf8')

function ruleBody(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return shellCss.match(new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\}`))?.[1] || ''
}

test('chat display readiness preserves the existing transcript reveal gate', () => {
  assert.match(chatView, /const displayReady = revealed \|\| showEmpty \|\| showLoadError/,
    'a transcript may hand off only after useScrollMode reveals it')
  assert.match(chatView, /useLayoutEffect\(\(\) => \{[\s\S]*onDisplayReadyRef\.current\?\.\(chatId\)/,
    'readiness must reach Shell before the browser paints the hidden transcript')
})

test('each pane holds one outgoing chat over one staging chat', () => {
  assert.match(shell, /layers\.push\(\{ paneId, chatId: previousId, role: 'held' \}\)/,
    'the transition keeps only the last painted chat in its pane')
  assert.match(shell, /role: transitioning \? 'staging' : 'active'/,
    'the destination stages only while a different painted chat exists')
  assert.match(shell, /pane\?\.activeTabKey !== `chat:\$\{id\}`/,
    'a stale ready signal from rapid navigation must not complete the handoff')
  // A held/staging chat is inert (settings covered OR not the active role); the
  // condition now also folds in a leaving pane during the exit beat (INV 9), so
  // match the leading clause rather than the exact full expression.
  assert.match(shell, /inert=\{settingsActive \|\| role !== 'active'/,
    'neither the held nor staging chat may accept interaction')
  assert.match(shell, /composerFocusRequest=\{role === 'active' \? composerFocusRequest : null\}/,
    'an inert staging composer must not consume the one-shot focus request')
})

test('the held chat is an opaque layer above staging until the atomic swap', () => {
  assert.match(ruleBody('.shell__chat-view'), /background:\s*var\(--bg\)/,
    'the cover must be opaque so hidden incoming content cannot leak through')
  assert.match(ruleBody('.shell__chat-view--staging'), /visibility:\s*visible/)
  assert.match(ruleBody('.shell__chat-view--staging'), /z-index:\s*1/)
  assert.match(ruleBody('.shell__chat-view--held'), /visibility:\s*visible/)
  assert.match(ruleBody('.shell__chat-view--held'), /z-index:\s*2/,
    'the last painted frame must stay above the staging mount')
})
