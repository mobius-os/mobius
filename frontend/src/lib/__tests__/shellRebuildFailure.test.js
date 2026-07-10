import test from 'node:test'
import assert from 'node:assert/strict'

import {
  shellRebuildFailureDetails,
  shellRebuildFailureMessage,
  summarizeShellRebuildFailure,
} from '../shellRebuildFailure.js'

test('summarizes Vite parser errors from noisy build output', () => {
  const output = [
    'vite v7.3.6 building client environment for production...',
    'transforming...',
    '✗ Build failed in 1.20s',
    '/data/platform/frontend/src/components/Shell/Shell.jsx:106:0: ERROR: Unexpected "<<"',
  ].join('\n')

  assert.equal(summarizeShellRebuildFailure(output), 'Unexpected "<<"')
  assert.equal(
    shellRebuildFailureMessage({ error: output }),
    'Shell rebuild failed: Unexpected "<<"',
  )
})

test('keeps permission errors actionable', () => {
  const output = 'Error: EACCES: permission denied, open \'/data/platform/frontend/node_modules/.vite-temp/vite.config.js.timestamp.mjs\''

  assert.match(shellRebuildFailureMessage(output), /EACCES/)
  assert.match(shellRebuildFailureMessage(output), /permission denied/)
})

test('falls back when no compiler details are present', () => {
  assert.equal(
    shellRebuildFailureMessage({}),
    'Shell rebuild failed. Previous shell is still running.',
  )
  assert.equal(shellRebuildFailureDetails({}), '')
})
