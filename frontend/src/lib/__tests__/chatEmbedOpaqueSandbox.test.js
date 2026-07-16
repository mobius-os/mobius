import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const appSource = readFileSync(new URL('../../App.jsx', import.meta.url), 'utf8')
const embedSource = readFileSync(
  new URL('../../components/ChatEmbed/ChatEmbed.jsx', import.meta.url),
  'utf8',
)
const runtimeSource = readFileSync(
  new URL('../../../public/mobius-runtime.js', import.meta.url),
  'utf8',
)
const navigationSource = readFileSync(
  new URL('../../hooks/useNavigation.js', import.meta.url),
  'utf8',
)
const indexSource = readFileSync(
  new URL('../../../index.html', import.meta.url),
  'utf8',
)

test('embed route bypasses IndexedDB-backed query persistence in opaque app frames', () => {
  const route = appSource.indexOf('if (isEmbedRoute())')
  const plainProvider = appSource.indexOf('<QueryClientProvider', route)
  const persistedProvider = appSource.indexOf('<PersistQueryClientProvider', route)
  assert.ok(route !== -1 && plainProvider > route)
  assert.ok(persistedProvider > plainProvider)
})

test('runtime passes only its verified scoped token through the correlated INIT', () => {
  assert.match(runtimeSource, /token = await getToken\(\)/)
  assert.match(runtimeSource, /if \(token\) msg\.token = token/)
  assert.match(runtimeSource, /w\.postMessage\(msg, '\*'\)/)
  assert.match(runtimeSource, /e\.source !== iframe\.contentWindow/)
  assert.match(runtimeSource, /e\.origin !== 'null'/)
  assert.match(runtimeSource, /if \(!msg\.instanceId\) \{\s*sendInit\(\)\.catch/)
})

test('nested renderer waits for an accepted app token before mounting ChatView', () => {
  assert.match(embedSource, /setTokenReady\(setEmbeddedToken\(msg\.token\)\)/)
  assert.match(embedSource, /if \(!chatId \|\| !tokenReady\)/)
  assert.ok(
    embedSource.indexOf('if (!chatId || !tokenReady)')
      < embedSource.indexOf('<ChatView'),
  )
})

test('opaque embed boot never depends on browser storage', () => {
  assert.match(navigationSource, /export const shellReload = \(\(\) => \{\s*try \{/)
  assert.match(indexSource, /isChatEmbed \|\| hasOwnerToken/)
  assert.match(indexSource, /window\.parent === window && 'serviceWorker' in navigator/)
})
