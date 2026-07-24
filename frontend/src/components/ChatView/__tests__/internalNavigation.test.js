import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { renderHook } from '../hooks/__tests__/react-hook-shim.mjs'
import useAppIntentNavigation from '../../Shell/useAppIntentNavigation.js'

const read = relative => readFileSync(new URL(relative, import.meta.url), 'utf8')

test('chat markdown intercepts only actionable unmodified shell-root clicks', () => {
  const source = read('../markdown/InlineContent.jsx')
  const clickHandler = source.slice(source.indexOf('onClick={(event) => {'))

  assert.match(clickHandler, /event\.metaKey/)
  assert.match(clickHandler, /event\.ctrlKey/)
  assert.match(clickHandler, /event\.shiftKey/)
  assert.match(clickHandler, /event\.altKey/)
  assert.match(clickHandler, /event\.button !== 0/)
  assert.match(clickHandler, /new URL\(href, window\.location\.href\)/)
  assert.match(clickHandler, /url\.origin === location\.origin/)
  assert.match(clickHandler, /\^\\\/shell\\\/\?\$\/\.test\(url\.pathname\)/)
  assert.match(clickHandler, /url\.searchParams\.has\('app'\)/)
  assert.match(clickHandler, /url\.searchParams\.has\('chat'\)/)
  assert.match(clickHandler, /event\.preventDefault\(\)/)
  assert.match(clickHandler, /onInternalNav\(url\)/)
  assert.match(source, /target="_blank"/)
  assert.match(source, /rel="noopener noreferrer"/)
})

test('shell intent callbacks keep identity while navTo changes per render', async () => {
  const calls = []
  let refreshedApps = []
  const navToRef = { current: (...args) => calls.push(['first', ...args]) }
  const params = {
    appsRef: { current: [{ id: 42, slug: 'artifacts' }] },
    refreshApps: async () => refreshedApps,
    showToast: (...args) => calls.push(['toast', ...args]),
    setAppIntents: (update) => calls.push(['intent', update({})]),
    navToRef,
  }
  const { result, rerender } = renderHook(useAppIntentNavigation, params)
  const firstOpen = result.current.openAppWithIntent
  const firstInternalNav = result.current.handleChatInternalNav

  navToRef.current = (...args) => calls.push(['latest', ...args])
  rerender({ ...params })

  assert.strictEqual(result.current.openAppWithIntent, firstOpen)
  assert.strictEqual(result.current.handleChatInternalNav, firstInternalNav)
  await result.current.openAppWithIntent('42', null)
  result.current.handleChatInternalNav(new URL('https://mobius.test/shell/?chat=c1'))
  assert.deepEqual(calls, [
    ['latest', 'canvas', { appId: 42 }],
    ['latest', 'chat', { chatId: 'c1' }],
  ])

  params.appsRef.current = []
  refreshedApps = [{ id: 43, slug: 'delayed' }]
  await result.current.openAppWithIntent('delayed', null, () => false)
  assert.equal(calls.length, 2, 'cancelled delayed resolution must not navigate')

  params.appsRef.current = []
  refreshedApps = []
  await result.current.openAppWithIntent('missing', null)
  assert.deepEqual(calls.at(-1), [
    'toast',
    'App is not installed yet.',
    { variant: 'info', duration: 6000 },
  ])
})

test('internal navigation crosses stable markdown memo boundaries', () => {
  const chatView = read('../ChatView.jsx')
  const message = read('../MsgContent.jsx')
  const renderer = read('../markdown/BlockRenderer.jsx')
  const blocks = read('../markdown/blocks.jsx')

  assert.match(chatView, /const handleInternalNav = useCallback/)
  assert.match(message, /prev\.onInternalNav === next\.onInternalNav/)
  assert.match(renderer, /<MemoBlock[\s\S]*onInternalNav=\{onInternalNav\}/)
  assert.match(blocks, /prev\.onInternalNav === next\.onInternalNav/)
})

test('shell resolves raw deep-link app targets through the intent rail', () => {
  const navigation = read('../../../hooks/useNavigation.js')
  const shell = read('../../Shell/Shell.jsx')
  const intentNavigation = read('../../Shell/useAppIntentNavigation.js')
  const pane = read('../../Shell/PaneChatView.jsx')

  assert.match(navigation, /const intent = params\.get\('intent'\)/)
  assert.match(navigation, /app,\s+appId:/)
  assert.match(intentNavigation, /const openAppWithIntent = useCallback/)
  assert.match(intentNavigation, /findAppForOpenTarget\(updatedApps, target\)/)
  assert.match(intentNavigation, /navToRef\.current\('canvas'/)
  assert.match(intentNavigation, /\}, \[refreshApps, showToast\]\)/)
  assert.match(intentNavigation, /\}, \[openAppWithIntent\]\)/)
  assert.match(shell, /if \(Number\.isFinite\(deepLink\.appId\)\)/)
  assert.match(shell, /navigationEpochRef\.current === startedAtEpoch/)
  const numericBoot = shell.slice(
    shell.indexOf('if (Number.isFinite(deepLink.appId))'),
    shell.indexOf('const startedAtEpoch'),
  )
  assert.doesNotMatch(numericBoot, /navTo|openAppWithIntent/)
  assert.match(shell, /onInternalNav=\{handleChatInternalNav\}/)
  assert.match(pane, /onInternalNav=\{onInternalNav\}/)
})
