import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = relative => readFileSync(new URL(relative, import.meta.url), 'utf8')

test('chat markdown intercepts only same-origin shell links on click', () => {
  const source = read('../markdown/InlineContent.jsx')
  const clickHandler = source.slice(source.indexOf('onClick={(event) => {'))

  assert.match(clickHandler, /new URL\(href, location\.origin\)/)
  assert.match(clickHandler, /url\.origin === location\.origin/)
  assert.match(clickHandler, /url\.pathname\.startsWith\('\/shell\/'\)/)
  assert.match(clickHandler, /event\.preventDefault\(\)/)
  assert.match(clickHandler, /onInternalNav\(url\)/)
  assert.match(source, /target="_blank"/)
  assert.match(source, /rel="noopener noreferrer"/)
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
  const pane = read('../../Shell/PaneChatView.jsx')

  assert.match(navigation, /const intent = params\.get\('intent'\)/)
  assert.match(navigation, /app,\s+appId:/)
  assert.match(shell, /const openAppWithIntent = useCallback/)
  assert.match(shell, /findAppForOpenTarget\(updatedApps, target\)/)
  assert.match(shell, /void openAppWithIntent\(deepLink\.app, deepLink\.intent\)/)
  assert.match(shell, /onInternalNav=\{handleChatInternalNav\}/)
  assert.match(pane, /onInternalNav=\{onInternalNav\}/)
})
