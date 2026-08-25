import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  ACTIONABLE_STATUSES,
  DISMISS_DX_PX,
  PLATFORM_REPO,
  actionableRecords,
  autopilotOnSend,
  contributeApp,
  contributeAppId,
  contributionReviewIntent,
  diffStatSummary,
  dismissKey,
  isDismissed,
  isHorizontalSwipe,
  passedDismissThreshold,
  publicationAction,
  rememberDismissed,
  rememberReviewItemDismissed,
  reviewDestinationLabel,
  reviewItemIntent,
  reviewItems,
  reviewPanelSummary,
  sendBlocker,
  statusLabel,
  visibleReviewItems,
  visibleRecords,
} from '../contributionReviewModel.js'

const cardSrc = readFileSync(new URL('../ContributionReviewCard.jsx', import.meta.url), 'utf8')
const clientSrc = readFileSync(new URL('../../../api/client.js', import.meta.url), 'utf8')
const cardCss = readFileSync(new URL('../ContributionReviewCard.css', import.meta.url), 'utf8')

const APPS = [
  { id: 3, slug: 'some-other-app' },
  { id: 8, slug: 'contribute' },
]

function fakeStorage(initial = {}) {
  const map = new Map(Object.entries(initial))
  return {
    getItem: key => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => { map.set(key, String(value)) },
    size: () => map.size,
  }
}

test('the ledger owner is resolved by slug, and a missing app hides the card', () => {
  assert.equal(contributeAppId(APPS), 8)
  assert.equal(contributeAppId([{ id: 3, slug: 'some-other-app' }]), null)
  assert.equal(contributeAppId([]), null)
  assert.equal(contributeAppId(undefined), null)
  assert.equal(contributeApp(APPS, 8).slug, 'contribute')
  assert.equal(contributeApp(APPS, 99), null)
})

test('only unsettled records reach the composer doorway', () => {
  const payload = { records: [
    { id: 'a', status: 'prepared' },
    { id: 'b', status: 'submitting' },
    { id: 'c', status: 'open' },
    { id: 'd', status: 'merged' },
    { id: 'e', status: 'closed' },
    { id: 'f' },
  ] }
  assert.deepEqual(actionableRecords(payload).map(record => record.id), ['a', 'b'])
  assert.deepEqual(actionableRecords(null), [])
  assert.deepEqual([...ACTIONABLE_STATUSES], ['prepared', 'submitting'])
})

test('every unsettled record remains reachable even when its current review needs attention', () => {
  const payload = { connected: false, records: [
    { id: 'ready', status: 'prepared', review: { state: 'ready' } },
    { id: 'drifted', status: 'prepared', review: { state: 'needs_refresh' } },
    { id: 'unreviewed', status: 'prepared' },
    { id: 'publishing', status: 'submitting' },
  ] }
  assert.deepEqual(
    visibleReviewItems(payload, fakeStorage()).map(item => item.id),
    ['ready', 'drifted', 'unreviewed', 'publishing'],
  )
})

test('record identities become opaque review intents, never routes', () => {
  assert.equal(contributionReviewIntent({ id: 'review.1-ready' }), 'review:review.1-ready')
  assert.equal(contributionReviewIntent({ id: '  review_2  ' }), 'review:review_2')
  assert.equal(contributionReviewIntent({ id: '../escape' }), null)
  assert.equal(contributionReviewIntent({ id: 'has spaces' }), null)
  assert.equal(contributionReviewIntent(null), null)

  assert.equal(
    reviewItemIntent({ kind: 'record', record: { id: 'single' } }),
    'review:single',
  )
  assert.equal(
    reviewItemIntent({ kind: 'stack', records: [{ id: 'layer-1' }, { id: 'layer-2' }] }),
    'review:layer-1',
  )
  assert.equal(reviewItemIntent({ kind: 'stack', records: [] }), null)
})

test('status and destination copy describe the next Contribute view', () => {
  assert.equal(statusLabel({ status: 'prepared' }), 'Review ready')
  assert.equal(statusLabel({
    status: 'prepared', quality_review_ready: true, review: { state: 'ready' },
  }), 'Ready to send')
  assert.equal(statusLabel({
    status: 'prepared', action: 'pr_update', quality_review_ready: true,
    review: { state: 'ready' },
  }), 'Ready to update')
  assert.equal(statusLabel({ status: 'prepared', stack: { id: 'demo' } }), 'Review together')
  assert.equal(statusLabel({ status: 'submitting' }), 'Publishing')
  assert.equal(statusLabel({ status: 'prepared', last_submit_error: 'failed' }), 'Needs attention')

  assert.equal(reviewDestinationLabel({ status: 'prepared' }), 'Review in Contribute')
  assert.equal(
    reviewDestinationLabel({ status: 'prepared', stack: { id: 'demo' } }),
    'Review stack in Contribute',
  )
  assert.equal(reviewDestinationLabel({ status: 'submitting' }), 'View in Contribute')
  assert.equal(
    reviewDestinationLabel({ status: 'prepared', last_submit_error: 'failed' }),
    'Resolve in Contribute',
  )
})

test('direct send requires the exact reviewed happy path', () => {
  const ready = {
    id: 'ready', status: 'prepared', quality_review_ready: true,
    review: { state: 'ready' },
  }
  assert.equal(sendBlocker(ready, { connected: true }), null)
  assert.match(sendBlocker({ ...ready, quality_review_ready: false }), /exact agent review/)
  assert.match(sendBlocker({ ...ready, review: { state: 'needs_refresh', message: 'Moved' } }), /Moved/)
  assert.match(sendBlocker({ ...ready, is_stack: true }), /linked set/)
  assert.match(sendBlocker(ready, { connected: false }), /Connect GitHub/)
  assert.equal(sendBlocker({ ...ready, status: 'submitting' }), null)

  assert.deepEqual(publicationAction(ready), {
    label: 'Send PR', busyLabel: 'Sending PR',
    progress: 'Opening the reviewed pull request…',
  })
  assert.deepEqual(publicationAction({ ...ready, action: 'pr_update' }), {
    label: 'Update PR', busyLabel: 'Updating PR',
    progress: 'Updating the reviewed pull request…',
  })
  assert.equal(autopilotOnSend({ autopilot_available: true }), true)
  assert.equal(autopilotOnSend({ autopilot_available: true, autopilot_default: false }), false)
  assert.equal(autopilotOnSend({ autopilot_available: false }), false)
})

test('multiple independent items share one centered bounded panel', () => {
  assert.match(cardSrc, /const panel = reviewPanelSummary\(pendingItems\.length\)/)
  assert.match(cardSrc, /const grouped = panel\.count > 1/)
  assert.match(cardSrc, /contrib-card-stack--grouped/)
  assert.match(cardSrc, /\{panel\.title\}/)
  assert.match(cardSrc, /\{panel\.copy\}/)
  assert.doesNotMatch(cardSrc, /contrib-card-stack__count/)
  assert.match(cardCss, /\.contrib-card-stack\s*\{[\s\S]*?width:\s*min\(100%, 640px\);[\s\S]*?margin-inline:\s*auto;/)
  assert.match(cardCss, /\.contrib-card-stack--grouped\s*\{[\s\S]*?max-height:\s*min\(52vh, 520px\);/)
  assert.match(cardCss, /\.contrib-card-stack--grouped \.contrib-card\s*\{[\s\S]*?border-radius:\s*0;/)
})

test('stack layers collapse into one ordered review item and one exact doorway', () => {
  const payload = { records: [
    { id: 'three', status: 'prepared', repo: PLATFORM_REPO, stack: {
      id: 'drawer', name: 'Drawer navigation', position: 3, total: 3,
    } },
    { id: 'single', status: 'prepared' },
    { id: 'one', status: 'prepared', repo: PLATFORM_REPO, stack: {
      id: 'drawer', name: 'Drawer navigation', position: 1, total: 3,
    } },
    { id: 'two', status: 'prepared', repo: PLATFORM_REPO, stack: {
      id: 'drawer', name: 'Drawer navigation', position: 2, total: 3,
    } },
  ] }
  const items = reviewItems(payload)
  assert.equal(items.length, 2)
  assert.equal(items[0].kind, 'stack')
  assert.deepEqual(items[0].records.map(record => record.id), ['one', 'two', 'three'])
  assert.equal(reviewItemIntent(items[0]), 'review:one')
  assert.equal(items[1].record.id, 'single')

  assert.match(cardSrc, /item\.kind === 'stack'/)
  assert.match(cardSrc, /<StackReviewRow/)
  assert.match(cardSrc, /Review stack in Contribute/)
  assert.doesNotMatch(cardSrc, />Layers</)
})

test('loaded older backends group canonical stack branches during hot reload', () => {
  const items = reviewItems({ records: [
    { id: 'a-second', status: 'prepared', repo: PLATFORM_REPO, is_stack: true,
      branch: 'stack/drawer-navigation/02-pins' },
    { id: 'z-first', status: 'prepared', repo: PLATFORM_REPO, is_stack: true,
      branch: 'stack/drawer-navigation/01-apps' },
  ] })
  assert.equal(items.length, 1)
  assert.equal(items[0].kind, 'stack')
  assert.equal(items[0].stack.name, 'Drawer navigation')
  assert.deepEqual(items[0].records.map(record => record.id), ['z-first', 'a-second'])
})

test('the grouped panel uses one stable explanation instead of redundant counts', () => {
  for (const count of [0, 1, 3]) {
    assert.deepEqual(reviewPanelSummary(count), {
      count,
      title: 'Reviews ready',
      copy: 'Each opens at its exact decision in Contribute.',
    })
  }
})

test('chat cards keep direct send and exact review on the same guarded routes', () => {
  assert.match(clientSrc, /submitter:\s*'chat-review-card'/)
  assert.match(clientSrc, /record\?\.action === 'pr_update'/)
  assert.match(clientSrc, /update-existing/)
  assert.match(cardSrc, /api\.contributions\.publish\(appId, record/)
  assert.match(cardSrc, /sending:\s*sending \|\| submitting/)
  assert.match(cardSrc, /\{sending \? action\.busyLabel : action\.label\}/)
  assert.match(cardSrc, />\s*Review\s*</)
  assert.doesNotMatch(cardSrc, /body_draft|record\.files|The exact text that will be published/)
  assert.doesNotMatch(cardSrc, /Contribute this improvement|>Details<|>Layers</)
  assert.match(cardSrc, /onOpenApp\(contributeApp, \{ final: true, intent \}\)/)
  assert.match(cardSrc, /contributionReviewIntent\(record\)/)
  assert.match(cardCss, /\.contrib-card__send\s*\{[\s\S]*?flex:\s*1;[\s\S]*?min-height:\s*42px;/)
  assert.match(cardCss, /\.contrib-card__review\s*\{[\s\S]*?min-height:\s*42px;/)
  assert.doesNotMatch(cardCss, /border-left:\s*2px/)
})

test('the floating card reduces a multi-line diff stat to its aggregate', () => {
  assert.equal(
    diffStatSummary(' frontend/src/a.js | 12 +++++\n backend/app/b.py | 3 ---\n 2 files changed, 12 insertions(+), 3 deletions(-)'),
    '2 files changed, 12 insertions(+), 3 deletions(-)',
  )
  assert.equal(diffStatSummary(' 1 file changed, 4 insertions(+)\n'), '1 file changed, 4 insertions(+)')
  assert.equal(diffStatSummary(null), '')
})

test('the card opts back into pointer events inside the pass-through foot', () => {
  assert.match(cardCss, /\.contrib-card-stack,\s*\n\.contrib-card \{\s*\n\s*pointer-events: auto;/)
})

test('only a decisively sideways drag counts, in either direction', () => {
  assert.equal(isHorizontalSwipe(20, 0), true)
  assert.equal(isHorizontalSwipe(-20, 0), true)
  assert.equal(isHorizontalSwipe(0, 30), false)
  assert.equal(isHorizontalSwipe(20, 19), false)
  assert.equal(isHorizontalSwipe(8, 0), false)
})

test('dismissal needs real horizontal travel', () => {
  assert.equal(passedDismissThreshold(DISMISS_DX_PX, 0), true)
  assert.equal(passedDismissThreshold(-DISMISS_DX_PX, 0), true)
  assert.equal(passedDismissThreshold(DISMISS_DX_PX - 1, 0), false)
  assert.equal(passedDismissThreshold(70, 90), false)
})

test('dismissal hides only this version without touching the ledger', () => {
  const record = { id: 'r1', status: 'prepared', updated_at: 'T1' }
  const store = fakeStorage()
  const payload = { records: [record] }
  assert.equal(visibleRecords(payload, store).length, 1)
  rememberDismissed(record, store)
  assert.equal(isDismissed(record, store), true)
  assert.equal(visibleRecords(payload, store).length, 0)
  assert.deepEqual(record, { id: 'r1', status: 'prepared', updated_at: 'T1' })
  assert.equal(isDismissed({ ...record, updated_at: 'T2' }, store), false)
})

test('dismissing a stack hides one card and any revised layer brings it back', () => {
  const storage = fakeStorage()
  const payload = { records: [
    { id: 'one', status: 'prepared', updated_at: '1', repo: PLATFORM_REPO,
      stack: { id: 'drawer', position: 1, total: 2 } },
    { id: 'two', status: 'prepared', updated_at: '1', repo: PLATFORM_REPO,
      stack: { id: 'drawer', position: 2, total: 2 } },
  ] }
  const item = visibleReviewItems(payload, storage)[0]
  assert.equal(rememberReviewItemDismissed(item, storage), true)
  assert.equal(visibleReviewItems(payload, storage).length, 0)
  payload.records[0].updated_at = '2'
  assert.equal(visibleReviewItems(payload, storage).length, 1)
})

test('dismissal degrades safely without usable storage', () => {
  const record = { id: 'r1', status: 'prepared', updated_at: 'T1' }
  const hostile = {
    getItem() { throw new Error('denied') },
    setItem() { throw new Error('denied') },
  }
  assert.equal(rememberDismissed(record, hostile), false)
  assert.equal(isDismissed(record, hostile), false)
  assert.equal(isDismissed(record, null), false)
  assert.equal(dismissKey({ updated_at: 'T1' }), null)
})

test('the swipe has a visible focusable equivalent', () => {
  assert.match(cardSrc, /className="contrib-card__dismiss"/)
  assert.match(cardSrc, /aria-label="Dismiss — keeps it in Contribute"/)
  assert.match(cardSrc, /onClick=\{\(\) => onDismiss\?\.\(\)\}/)
  assert.match(cardSrc, /import \{ X \} from '@openai\/apps-sdk-ui\/components\/Icon'/)
  assert.equal((cardSrc.match(/<X width=\{14\} height=\{14\} aria-hidden="true" \/>/g) || []).length, 2)
})

test('the dismissal gesture is claimed with a non-passive touchmove', () => {
  assert.match(cardSrc, /addEventListener\('touchmove', onMove, \{ passive: false \}\)/)
  assert.match(cardSrc, /event\.preventDefault\(\)/)
  assert.doesNotMatch(cardSrc, /onTouchMove=\{/)
})

test('every card shape shares one swipe implementation', () => {
  assert.equal((cardSrc.match(/function useSwipeToDismiss\(/g) || []).length, 1)
  assert.equal((cardSrc.match(/= useSwipeToDismiss\(onDismiss\)/g) || []).length, 2)
  assert.equal((cardSrc.match(/addEventListener\('touchmove'/g) || []).length, 1)
})
