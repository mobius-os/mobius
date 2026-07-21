import test from 'node:test'
import assert from 'node:assert/strict'

import {
  appBuildFailureMessage,
  appUpdateStaleMessage,
  findAppStoreApp,
  summarizeAppBuildFailure,
} from '../appRecovery.js'

test('formats app build failures with app name and reassurance', () => {
  assert.equal(
    appBuildFailureMessage({
      appName: 'Planner',
      summary: 'Expected "}" but found end of file',
    }),
    'Couldn\'t compile Planner — Expected "}" but found end of file. The previous version is still running.',
  )
})

test('preserves terminal punctuation and uses a natural fallback name', () => {
  assert.equal(
    appBuildFailureMessage({ summary: 'Parser stopped.' }),
    'Couldn\'t compile this app — Parser stopped. The previous version is still running.',
  )
})

test('compacts and truncates app build summaries', () => {
  const summary = summarizeAppBuildFailure({
    summary: `Unexpected ${'x'.repeat(220)}`,
  })
  assert.equal(summary.length, 160)
  assert.match(summary, /…$/)
})

test('explains how to recover a stale update without alarming about live state', () => {
  assert.equal(
    appUpdateStaleMessage({ appName: 'Reflection' }),
    'The pending update for Reflection changed upstream. Review the latest update and start again. The previous version is still running.',
  )
})

test('finds the canonical App Store before falling back to its display name', () => {
  const nameFallback = {
    id: 1,
    name: 'App Store',
    manifest_url: 'https://example.test/lookalike',
  }
  const misleadingUrl = {
    id: 2,
    name: 'Lookalike',
    manifest_url: (
      'https://example.test/?next='
      + 'https://raw.githubusercontent.com/mobius-os/app-store/'
    ),
  }
  const canonical = {
    id: 3,
    name: 'Store',
    manifest_url: 'https://raw.githubusercontent.com/mobius-os/app-store/main#manifest-id=store',
  }

  assert.equal(findAppStoreApp([nameFallback, misleadingUrl, canonical]), canonical)
  assert.equal(findAppStoreApp([misleadingUrl, nameFallback]), nameFallback)
  assert.equal(findAppStoreApp(null), null)
})
