import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { samePaneChatProps } from '../paneChatProps.js'

// Pane isolation has two halves and both are silently revertible: the comparator
// must bail when nothing this chat cares about moved, and Shell must not hand it
// a prop whose identity churns every render. A regression in either half leaves
// the app correct and just quietly rerenders every open transcript on any chat's
// run event, so nothing else in the suite would notice.

const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
const useTheme = readFileSync(
  new URL('../../../hooks/useTheme.js', import.meta.url), 'utf8',
)

// A realistic prop bag containing the identities Shell passes.
function propBag(overrides = {}) {
  const stable = {
    onComposerRequestHandled: () => {},
    onSystemEvent: () => {},
    markStreamingStart: () => {},
    markStreamingEnd: () => {},
    refreshApps: () => {},
    refreshChats: () => {},
    markChatOwnerActivity: () => {},
    loadTheme: () => {},
    openAppWithIntent: () => {},
    onInternalNav: () => {},
    onChatMissing: () => {},
    onFirstMessage: () => {},
    onDisplayReady: () => {},
  }
  return {
    chatId: 'chat-a',
    paneId: 'pane-1',
    runtimeActive: true,
    keepTranscriptPainted: false,
    paneContentHeight: 640,
    externalRunSignal: { startedAt: 1, activityAt: 2 },
    composerRequest: null,
    ...stable,
    ...overrides,
  }
}

test('an identity-stable prop bag does not rerender this pane', () => {
  const previous = propBag()
  assert.equal(samePaneChatProps(previous, { ...previous }), true)
})

test('every prop is compared by identity', () => {
  const previous = propBag()
  // Driven off the bag's own keys so a prop added later is covered without
  // editing this test — the realistic way isolation regresses is a new prop.
  for (const key of Object.keys(previous)) {
    const churned = typeof previous[key] === 'function'
      ? () => {}
      : { churned: key }
    assert.equal(
      samePaneChatProps(previous, { ...previous, [key]: churned }), false,
      `a changed ${key} must rerender the pane`,
    )
  }
})

test('an added or removed prop rerenders rather than being skipped', () => {
  const previous = propBag()
  const { paneContentHeight, ...withoutOne } = previous
  assert.equal(samePaneChatProps(previous, withoutOne), false)
  assert.equal(samePaneChatProps(withoutOne, previous), false)
})

test('Shell hands the pane only identity-stable props', () => {
  const open = shell.indexOf('<PaneChatView')
  assert.ok(open > 0, 'Shell must render PaneChatView')
  const slice = shell.slice(open, shell.indexOf('/>', open))
  // An inline arrow, function, object, or array literal is freshly allocated on
  // every Shell render, so the comparator above can never bail out.
  assert.doesNotMatch(slice, /=\{\s*\(?[^}]*=>/,
    'no pane prop may be an inline arrow — hoist it into a useCallback')
  assert.doesNotMatch(slice, /=\{\{/,
    'no pane prop may be an inline object literal — useMemo it')
  assert.doesNotMatch(slice, /=\{\[/,
    'no pane prop may be an inline array literal — useMemo it')
  // The intent navigator is a stable callback and preserves this pane while it
  // opens the app at an exact in-app review.
  assert.match(slice, /openAppWithIntent=\{openAppWithIntent\}/)
  assert.doesNotMatch(slice, /apps=\{apps\}/,
    'chat artifact queries must not make every app-list refresh rerender the pane')
  assert.doesNotMatch(shell, /const stablePaneNavTo = useCallback\(/)
  // loadTheme is a dependency of handleSystemEvent, which is itself a pane prop.
  assert.match(useTheme, /const loadTheme = useCallback\(/)
})
