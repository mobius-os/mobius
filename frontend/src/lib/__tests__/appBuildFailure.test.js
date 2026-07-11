import test from 'node:test'
import assert from 'node:assert/strict'

import {
  appBuildFailureMessage,
  summarizeAppBuildFailure,
} from '../appBuildFailure.js'

test('formats app build failures with app name and reassurance', () => {
  assert.equal(
    appBuildFailureMessage({
      appName: 'Planner',
      summary: 'Expected "}" but found end of file',
    }),
    'Couldn\'t compile Planner — Expected "}" but found end of file. The previous version is still running.',
  )
})

test('compacts and truncates app build summaries', () => {
  const summary = summarizeAppBuildFailure({
    summary: `Unexpected ${'x'.repeat(220)}`,
  })
  assert.equal(summary.length, 160)
  assert.match(summary, /…$/)
})
