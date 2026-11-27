import assert from 'node:assert/strict'
import { test } from 'node:test'
import { waitNeedsOwner } from '../handoffSummary.js'
import { restartOnlyOffered, waitPresentation } from '../waitingPresentation.js'

const activation = (extra = {}) => ({
  kind: 'platform_activation', status: 'armed',
  description: 'Load the exact committed platform changes',
  next_check_at: new Date(Date.now() + 30_000).toISOString(), interval_secs: 60,
  ...extra,
})

test('a restart that was only offered reads as the owner\'s move, not a check in progress', () => {
  const wait = activation({ restart_requested: false })
  assert.equal(restartOnlyOffered(wait), true)
  assert.equal(waitNeedsOwner(wait), true)
  const shown = waitPresentation(wait)
  assert.equal(shown.summary, 'press Restart now on the card')
  assert.doesNotMatch(`${shown.summary} ${shown.checker}`, /next check|every/i)
})

test('a restart the owner asked for keeps its waiting presentation', () => {
  const wait = activation({ restart_requested: true })
  assert.equal(restartOnlyOffered(wait), false)
  assert.equal(waitNeedsOwner(wait), false)
  assert.match(waitPresentation(wait).summary, /next check|every/i)
})

test('a server that does not say whether the restart was requested changes nothing', () => {
  const wait = activation()
  assert.equal(restartOnlyOffered(wait), false)
  assert.equal(waitNeedsOwner(wait), false)
})

test('an ordinary wait is never treated as an offered restart', () => {
  assert.equal(restartOnlyOffered({ kind: 'timer', restart_requested: false }), false)
})
