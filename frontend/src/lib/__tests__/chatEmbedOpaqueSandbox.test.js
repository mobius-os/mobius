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
const bootstrapSource = readFileSync(
  new URL('../chatEmbedBootstrap.js', import.meta.url),
  'utf8',
)
const indexSource = readFileSync(
  new URL('../../../index.html', import.meta.url),
  'utf8',
)

test('embed route bypasses IndexedDB-backed query persistence in opaque app frames', () => {
  const route = appSource.indexOf('if (EMBED_ROUTE)')
  const plainProvider = appSource.indexOf('<QueryClientProvider', route)
  const persistedProvider = appSource.indexOf('<PersistQueryClientProvider', route)
  assert.ok(route !== -1 && plainProvider > route)
  assert.ok(persistedProvider > plainProvider)
  assert.match(
    appSource.slice(route, persistedProvider),
    /<Suspense fallback=\{null\}>/,
    'the opaque embed must stay blank while its route chunk and capability load',
  )
})

test('runtime INIT carries a one-use bootstrap rather than an owner or app token', () => {
  assert.match(runtimeSource, /bootstrapCapability: capability/)
  assert.match(runtimeSource, /authorizationId/)
  assert.match(runtimeSource, /targetFrame\.contentWindow\.postMessage\(msg, '\*'\)/)
  assert.match(runtimeSource, /e\.source !== iframe\.contentWindow/)
  assert.match(runtimeSource, /e\.origin !== 'null'/)
  assert.doesNotMatch(runtimeSource, /msg\.token\s*=/)
})

test('lazy embed route announces receiver readiness before the runtime mints a grant', () => {
  assert.match(appSource, /beginEmbedBootstrap\(\)/)
  assert.match(bootstrapSource, /event\.source !== window\.parent/)
  assert.match(embedSource, /handoffEmbedBootstrap\(onMessage\)/)
  assert.match(embedSource, /postMessage\(\{ type: BOOTSTRAP_READY \}, '\*'\)/)
  assert.match(runtimeSource, /msg\.type === EMBED_BOOTSTRAP_READY/)
  assert.doesNotMatch(runtimeSource, /addEventListener\('load', sendInit\)/)
})

test('nested renderer stays inert until the server verifies the bootstrap', () => {
  assert.match(embedSource, /\/api\/app-chat-embeds\/session/)
  assert.match(embedSource, /setEphemeralAuthSession\(session\.token, msg\.instanceId\)/)
  assert.match(embedSource, /if \(!authorized \|\| !chatId\) return <div className="chat-embed" aria-hidden="true"/)
  assert.ok(
    embedSource.indexOf('if (!authorized || !chatId)')
      < embedSource.indexOf('<ChatView'),
  )
})

test('opaque embed boot skips service workers and owner browser storage', () => {
  assert.match(appSource, /beginEphemeralAuth\(\)/)
  assert.match(indexSource, /window\.location\.pathname !== '\/shell\/embed\/chat'/)
  assert.doesNotMatch(embedSource, /localStorage\.(?:getItem|setItem|removeItem)/)
})
