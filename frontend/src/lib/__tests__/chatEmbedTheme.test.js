/** Source-level guards for the inert opaque embedded-chat theme handoff. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const SOURCE = readFileSync(
  new URL('../../components/ChatEmbed/ChatEmbed.jsx', import.meta.url),
  'utf8',
)
const APP_SOURCE = readFileSync(new URL('../../App.jsx', import.meta.url), 'utf8')

test('ChatEmbed does not load owner theme/storage before authorization', () => {
  assert.doesNotMatch(SOURCE, /\buseTheme\s*\(/)
  assert.doesNotMatch(SOURCE, /localStorage\s*\./)
  assert.match(APP_SOURCE, /if \(EMBED_ROUTE\) \{[\s\S]*beginEphemeralAuth\(\)/)
})

test('ChatEmbed applies theme returned by the verified session exchange', () => {
  const verified = SOURCE.indexOf("session.role !== 'participant'")
  const applied = SOURCE.indexOf('applyThemeToDom(')
  const authorized = SOURCE.indexOf('setAuthorized(true)')
  assert.ok(verified !== -1 && applied > verified)
  assert.ok(authorized > applied)
  assert.match(SOURCE, /session\.theme\?\.css/)
})

test('ChatEmbed stays blank until the server capability exchange succeeds', () => {
  const blank = SOURCE.indexOf('if (!authorized || !chatId)')
  const chatView = SOURCE.indexOf('<ChatView')
  const exchange = SOURCE.indexOf('/api/app-chat-embeds/session')
  assert.ok(exchange !== -1, 'server capability exchange exists')
  assert.ok(blank !== -1 && blank < chatView, 'blank authorization gate precedes ChatView')
  assert.match(SOURCE.slice(blank, chatView), /aria-hidden="true"/)
})

test('ChatEmbed accepts no chat id or credential from its URL', () => {
  assert.doesNotMatch(SOURCE, /location\.search|URLSearchParams/)
  assert.match(SOURCE, /bootstrapCapability/)
  assert.match(SOURCE, /setEphemeralAuthSession/)
})
