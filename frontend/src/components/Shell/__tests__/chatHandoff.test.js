import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
const shellCss = readFileSync(new URL('../Shell.css', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../../ChatView/ChatView.jsx', import.meta.url), 'utf8')
const apiClient = readFileSync(new URL('../../../api/client.js', import.meta.url), 'utf8')

function ruleBody(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return shellCss.match(new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\}`))?.[1] || ''
}

test('chat display readiness trusts settled cache but keeps cold/running gates', () => {
  assert.match(
    chatView,
    /const cachedDisplayReady = initialEntryPhase === 'cached' && revealed/,
    'a settled cached transcript can hand off without waiting on network freshness',
  )
  assert.match(
    chatView,
    /const displayReady = cachedDisplayReady\s*\|\| \(!loading && \(revealed \|\| showEmpty \|\| showLoadError\)\)/,
    'cold and running transcripts keep the authoritative load and reveal gates',
  )
  assert.match(chatView, /useLayoutEffect\(\(\) => \{[\s\S]*onDisplayReady\?\.\(chatId\)/,
    'readiness must reach Shell before the browser paints the hidden transcript')
  assert.match(chatView,
    /if \(displayReady\) onDisplayReady\?\.\(chatId\)[\s\S]*\}, \[chatId, displayReady, onDisplayReady\]\)/,
    'an already-ready chat must re-announce when a cross-pane move changes its handoff owner')
  assert.doesNotMatch(chatView, /onDisplayReadyRef/,
    'the callback dependency is the owner-change signal; a parallel mutable ref would obscure it')
})

test('a staging chat cannot leave the outgoing transcript held on a wedged request', () => {
  assert.match(chatView, /const CHAT_FETCH_TIMEOUT_MS = 15000/)
  assert.match(
    chatView,
    /apiFetch\(`\/chats\/\$\{chatId\}\?limit=20&compact=1`, \{\s*timeoutMs: CHAT_FETCH_TIMEOUT_MS,\s*signal: initialLoadController\.signal,\s*\}\)/,
    'the initial load must share the bounded message-fetch deadline',
  )
  assert.match(chatView, /initialLoadController\.abort\(\)/,
    'hiding or unmounting a staging chat must release its request immediately')
  assert.match(apiClient, /AbortSignal\.any\(\[signal, ctrl\.signal\]\)/,
    'apiFetch must compose lifecycle cancellation with its deadline')
  assert.match(apiClient, /error\.name = 'TimeoutError'\s*ctrl\.abort\(error\)/,
    'a deadline remains distinguishable from routine lifecycle cancellation')
  assert.match(apiClient, /if \(error\?\.name !== 'AbortError'\) void verifyConnectivity\(\)/,
    'switching panes must not trigger a redundant connectivity probe')
})

test('each pane holds one outgoing chat over one staging chat', () => {
  assert.match(shell, /layers\.push\(\{ paneId, chatId: previousId, role: 'held' \}\)/,
    'the transition keeps only the last painted chat in its pane')
  assert.match(shell, /role: transitioning \? 'staging' : 'active'/,
    'the destination stages only while a different painted chat exists')
  assert.match(
    shell,
    /paneModel\.activeKeyForOwner\(workspaceStateRef\.current\.ws, paneKey\) !== `chat:\$\{id\}`/,
    'late readiness must be validated against either a real pane or the synthetic single owner',
  )
  // A held/staging chat is inert (the takeover is PAINTING OR not the active role);
  // the condition now also folds in a leaving pane during the exit beat (INV 9), so
  // match the leading clause rather than the exact full expression. The takeover
  // gate is the EFFECTIVE-mode `settingsOverlay` (finding F3), not the committed one.
  assert.match(shell, /inert=\{settingsOverlay \|\| role !== 'active'/,
    'neither the held nor staging chat may accept interaction')
  assert.match(shell, /composerRequest=\{role === 'active' \? composerRequest : null\}/,
    'an inert staging composer must not consume a one-shot composer request')
})

test('app-supplied drafts update retained composers as well as remounted chats', () => {
  assert.match(shell,
    /navTo\('chat', \{ chatId: e\.data\.chatId \}\)[\s\S]*requestComposer\(e\.data\.chatId, \{ draft: draftText \}\)/,
    'the open-chat handoff must target the live composer after navigation')
  assert.match(chatView,
    /typeof composerRequest\.draft === 'string'[\s\S]*handleComposerInputChange\(composerRequest\.draft\)/,
    'a retained ChatView must apply the requested draft to controlled state')
  assert.match(chatView, /if \(!composerRequest\.focus\) \{[\s\S]*onComposerRequestHandled\?\.\(token\)/,
    'a draft-only handoff must settle without stealing focus')
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
