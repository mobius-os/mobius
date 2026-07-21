/**
 * Unit test for ProviderAuth's "doesn't run its own auth query"
 * contract (Ticket 036 commit 3b).
 *
 * Run with:
 *   cd frontend && node --test src/components/ProviderAuth/__tests__/ProviderAuth.test.js
 *
 * Why a static source check, not a renderer test:
 *   ProviderAuth is a real React component and we don't have a
 *   renderer hooked up under node:test (we'd need jsdom +
 *   testing-library, neither installed). The actually-load-bearing
 *   property after 036 is the absence of a useQuery call — that's
 *   what makes "auth state lives in one place per render tree"
 *   true. A grep of the source file directly asserts the property
 *   without infrastructure cost.
 *
 *   If a future refactor reintroduces useQuery in ProviderAuth,
 *   this test fails with a useful message pointing back at the
 *   ticket.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const SOURCE = readFileSync(
  resolve(HERE, '..', 'ProviderAuth.jsx'),
  'utf8',
)

test('ProviderAuth does NOT call useQuery internally', () => {
  // After 036 commit 3b, the `authenticated` fact comes in via
  // props from SettingsView and SetupWizard's ProviderStep. The
  // canonical useQuery for claude-status lives at the parent
  // level so the same render tree never reads the same fact
  // twice. A regression that reinstates useQuery would silently
  // bring back the duplication.
  assert.ok(!/\buseQuery\b/.test(SOURCE),
    `ProviderAuth.jsx must not call useQuery. After ticket 036 commit 3b, the parent (SettingsView or SetupWizard) owns the claude-status query and passes \`authenticated\` as a prop.`)
})

test('ProviderAuth does NOT call any *.useQuery() factory either', () => {
  // The queries.js wrappers (e.g. `authQueries.provider.claudeStatus.useQuery`)
  // are how the rest of the app calls useQuery indirectly. Match
  // these too — a regression that goes through the factory has the
  // same observable effect as direct useQuery.
  assert.ok(!/\.useQuery\(/.test(SOURCE),
    `ProviderAuth.jsx must not call any *.useQuery() factory. See ticket 036 commit 3b.`)
})

test('ProviderAuth exports a component that accepts an `authenticated` prop', () => {
  // Locks in the prop contract so a future refactor that renames
  // the prop without updating consumers fails loudly here.
  assert.ok(/function ProviderAuth\([^)]*\bauthenticated\b/.test(SOURCE),
    `ProviderAuth must take an \`authenticated\` prop (per 036 commit 3b prop-drilling contract).`)
})

test('ProviderAuth treats a successful code exchange as authoritative', () => {
  // /provider/code returns success only after credentials are durable. A
  // synchronous fetchQuery can reuse a fresh cached `false` value (or an
  // in-flight stale request) and reject a valid one-shot code. Publish the
  // committed state, then revalidate it in the background.
  assert.ok(/setQueryData\(/.test(SOURCE),
    'ProviderAuth must publish successful authentication to the shared cache.')
  assert.ok(/invalidate\(queryClient\)/.test(SOURCE),
    'ProviderAuth must revalidate the authoritative cache update in the background.')
  assert.ok(!/fetchQuery\(/.test(SOURCE),
    'ProviderAuth must not synchronously gate a successful code exchange on cached status.')
})
