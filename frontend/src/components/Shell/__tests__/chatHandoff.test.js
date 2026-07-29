import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
const shellCss = readFileSync(new URL('../Shell.css', import.meta.url), 'utf8')
const drawerCss = readFileSync(new URL('../../Drawer/Drawer.css', import.meta.url), 'utf8')
const chatSurfaceModel = readFileSync(new URL('../chatSurfaceModel.js', import.meta.url), 'utf8')
const workspaceChrome = readFileSync(new URL('../WorkspaceChrome.jsx', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../../ChatView/ChatView.jsx', import.meta.url), 'utf8')
const apiClient = readFileSync(new URL('../../../api/client.js', import.meta.url), 'utf8')

function ruleBody(selector, source = shellCss) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return source.match(new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\}`))?.[1] || ''
}

test('chat display readiness preserves the authoritative transcript reveal gate', () => {
  assert.match(
    chatView,
    /const displayReady = !loading && \(revealed \|\| showEmpty \|\| showLoadError\)/,
    'a cached transcript cannot paint before the live chat read confirms it',
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
  assert.match(chatSurfaceModel, /chatId: previousId,[\s\S]*role: 'held'/,
    'the transition keeps only the last painted chat in its pane')
  assert.match(chatSurfaceModel, /role: transitioning \? 'staging' : 'active'/,
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
  assert.match(shell, /inert=\{!surfaceVisible \|\| settingsOverlay \|\| role !== 'active'/,
    'neither the held nor staging chat may accept interaction')
  assert.match(shell, /composerRequest=\{role === 'active' && surfaceVisible \? composerRequest : null\}/,
    'an inert staging composer must not consume a one-shot composer request')
})

test('only the painted workspace world can expose its handoff layers', () => {
  assert.match(
    shell,
    /const paneActiveKey = paneModel\.activeKeyForOwner\(workspace, paneId\) \|\| tabKey/,
    'handoff visibility must follow the owner current content, including the synthetic single-screen owner',
  )
  assert.match(
    shell,
    /const surfaceVisible = !!\(underlay \|\| paned \|\| fullBleed\)/,
    'a retained owner in the hidden world must remain mounted without becoming visible',
  )
  assert.match(
    shell,
    /const handoffClass = !settingsOverlay && surfaceVisible && role !== 'active'/,
    'held and staging visibility classes belong only to the world that is actually painting',
  )
  assert.match(
    shell,
    /data-chat-surface=\{surfaceVisible && role === 'active' \? 'painted' : undefined\}/,
    'browser contracts need one explicit selector for the settled interactive chat surface',
  )
  assert.match(
    shell,
    /inert=\{!surfaceVisible \|\| settingsOverlay \|\| role !== 'active'/,
    'a retained chat in the parked workspace world must remain inert',
  )
  assert.match(
    shell,
    /aria-hidden=\{!surfaceVisible \|\| settingsOverlay \|\| role !== 'active'/,
    'a retained chat in the parked workspace world must leave the accessibility tree',
  )
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

test('direct desktop chat opens hand focus to the destination composer', () => {
  assert.match(shell,
    /startupChatComposerFocusPendingRef = useRef\([\s\S]*activeView === 'chat' && effectiveViewMode === 'single'[\s\S]*\)/,
    'only a restored single-screen chat may retain the startup focus intent')
  assert.match(shell,
    /if \(!startupChatComposerFocusPendingRef\.current\) return[\s\S]*activeView !== 'chat' \|\| activeChatId == null[\s\S]*startupChatComposerFocusPendingRef\.current = false[\s\S]*focusDesktopChatPaneComposer\(activeChatId\)/,
    'restored chat focus must be one-shot rather than following later mode changes')
  assert.match(shell,
    /newChat\(\{ focusComposer: true, recordHistory: true \}\)/,
    'the direct New chat action must request composer focus')
  assert.match(shell,
    /if \(focusComposer\) requestComposer\(chatId, \{ focus: true \}\)/,
    'New chat must deliver its focus request after the destination is known')
  const selectChat = shell.match(/function selectChat\(id\) \{([\s\S]*?)\n  \}/)?.[1] || ''
  assert.match(selectChat,
    /navTo\('chat', \{ chatId: id \}\)[\s\S]*focusDesktopChatPaneComposer\(id\)/,
    'drawer and settings chat selection must focus after requesting navigation')
  assert.match(shell,
    /onActivate=\{\(\) => \{[\s\S]*tabModel\.tabNavTarget\(tab\)[\s\S]*navTo\(view, opts\)[\s\S]*tab\.kind === 'chat'[\s\S]*focusDesktopChatPaneComposer\(tab\.id\)/,
    'the single-pane tab strip must focus a selected chat composer')
  const selectedChatHandoffs = workspaceChrome.match(
    /if \(tab\.kind === 'chat'\) onChatPaneSelected\?\.\(tab\.id\)/g,
  ) || []
  assert.equal(selectedChatHandoffs.length, 2,
    'both active-tab and tab-switch paths in a tiled pane must focus chat composers')
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

test('chat selection settles without flashing or crossfading text layers', () => {
  const drawerItem = ruleBody('.drawer__item', drawerCss)
  assert.equal(
    drawerItem.match(/transition:\s*([^;]+);/)?.[1],
    'background-color 0.12s',
    'the selected title color must snap instead of tweening its glyphs')

  assert.match(ruleBody('.shell__chat-view > .chat'), /transition:\s*opacity 90ms ease-out/,
    'the ready transcript should settle on its existing mounted surface')
  assert.match(ruleBody('.shell__chat-view--staging > .chat'), /opacity:\s*0\.94/,
    'only the incoming transcript should be softened while the opaque cover is held')
  assert.doesNotMatch(ruleBody('.shell__chat-view--held'), /opacity:/,
    'the outgoing transcript must stay opaque instead of ghosting over incoming text')
  assert.match(shellCss,
    /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.shell__chat-view > \.chat \{\s*transition:\s*none;/,
    'the transcript settle should disappear under reduced motion')
  assert.match(drawerCss,
    /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.drawer__item \{\s*transition:\s*none;/,
    'the drawer background wash should disappear under reduced motion')
})
