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
  contributeLabel,
  diffStatSummary,
  payoffLine,
  dismissKey,
  isDismissed,
  isHorizontalSwipe,
  passedDismissThreshold,
  rememberDismissed,
  rememberReviewItemDismissed,
  reviewItems,
  reviewPanelSummary,
  sendBlocker,
  statusLabel,
  visibleReviewItems,
  visibleRecords,
} from '../contributionReviewModel.js'

const cardSrc = readFileSync(
  new URL('../ContributionReviewCard.jsx', import.meta.url), 'utf8',
)
const clientSrc = readFileSync(
  new URL('../../../api/client.js', import.meta.url), 'utf8',
)
const cardCss = readFileSync(
  new URL('../ContributionReviewCard.css', import.meta.url), 'utf8',
)

const APPS = [
  { id: 3, slug: 'some-other-app' },
  { id: 8, slug: 'contribute' },
]

test('the ledger owner is resolved by slug, and a missing app hides the card', () => {
  assert.equal(contributeAppId(APPS), 8)
  assert.equal(contributeAppId([{ id: 3, slug: 'some-other-app' }]), null)
  assert.equal(contributeAppId([]), null)
  assert.equal(contributeAppId(undefined), null)
  assert.equal(contributeApp(APPS, 8).slug, 'contribute')
  assert.equal(contributeApp(APPS, 99), null)
})

test('only records awaiting an owner decision reach the composer', () => {
  const payload = { records: [
    { id: 'a', status: 'prepared' },
    { id: 'b', status: 'submitting' },
    { id: 'c', status: 'open' },
    { id: 'd', status: 'merged' },
    { id: 'e', status: 'closed' },
    { id: 'f' },
  ] }
  assert.deepEqual(actionableRecords(payload).map(r => r.id), ['a', 'b'])
  assert.deepEqual(actionableRecords(null), [])
  assert.deepEqual([...ACTIONABLE_STATUSES], ['prepared', 'submitting'])
})

test('a ready prepared record is sendable', () => {
  const record = {
    status: 'prepared', review: { state: 'ready', message: 'Still matches' },
  }
  assert.equal(sendBlocker(record, { connected: true }), null)
})

test('a drifted record cannot be sent, and says why in the server’s words', () => {
  const record = {
    status: 'prepared',
    review: { state: 'needs_refresh', message: 'The branch moved since you reviewed it.' },
  }
  assert.equal(
    sendBlocker(record, { connected: true }),
    'The branch moved since you reviewed it.',
  )
})

test('a verdict with no message still blocks, with a generic reason', () => {
  const record = { status: 'prepared', review: { state: 'needs_refresh' } }
  assert.match(sendBlocker(record, { connected: true }), /prepared again/)
})

test('a disconnected GitHub blocks Send before any request is made', () => {
  const record = { status: 'prepared', review: { state: 'ready' } }
  assert.match(sendBlocker(record, { connected: false }), /Connect GitHub/)
})

test('a stack layer is never sendable from chat — the chain is reviewed together', () => {
  const record = {
    status: 'prepared', stack: { id: 'demo', position: 1, total: 2 },
    review: { state: 'ready' },
  }
  assert.match(sendBlocker(record, { connected: true }), /stacked set/)
})

// The submit endpoint re-runs every check, but a one-tap public action must not
// present an absent preflight as ready. Submitting is already in flight and does
// not need another blocker.
test('an absent prepared verdict fails closed', () => {
  assert.match(
    sendBlocker({ status: 'prepared' }, { connected: true }),
    /Open Contribute/,
  )
  assert.equal(sendBlocker({ status: 'submitting' }, { connected: true }), null)
  assert.equal(sendBlocker(null, { connected: true }), null)
})

test('autopilot on send mirrors the owner default and the backend capability', () => {
  assert.equal(autopilotOnSend({ autopilot_available: true }), true)
  assert.equal(
    autopilotOnSend({ autopilot_available: true, autopilot_default: true }), true,
  )
  assert.equal(
    autopilotOnSend({ autopilot_available: true, autopilot_default: false }), false,
  )
  // An older backend that cannot run the loop must never have one granted.
  assert.equal(autopilotOnSend({ autopilot_default: true }), false)
  assert.equal(autopilotOnSend(null), false)
})

test('the status word distinguishes waiting and in-flight', () => {
  assert.equal(statusLabel({ status: 'prepared' }), 'Ready to contribute')
  assert.equal(
    statusLabel({ status: 'prepared', stack: { id: 'demo' } }),
    'Review together',
  )
  assert.equal(statusLabel({ status: 'submitting' }), 'Publishing')
})

test('records that need attention stay in Contribute instead of blocking chat', () => {
  const payload = {
    connected: true,
    records: [
      { id: 'ready', status: 'prepared', review: { state: 'ready' } },
      { id: 'drifted', status: 'prepared', review: { state: 'needs_refresh' } },
      { id: 'unreviewed', status: 'prepared' },
      { id: 'publishing', status: 'submitting' },
    ],
  }
  assert.deepEqual(
    visibleReviewItems(payload, fakeStorage()).map(item => item.id),
    ['ready', 'publishing'],
  )
  assert.deepEqual(
    visibleReviewItems({ ...payload, connected: false }, fakeStorage())
      .map(item => item.id),
    ['publishing'],
  )
  // Filtering is presentation-only: the durable ledger still owns every item.
  assert.deepEqual(
    actionableRecords(payload).map(record => record.id),
    ['ready', 'drifted', 'unreviewed', 'publishing'],
  )
})

test('multiple independent review items share one bounded review panel', () => {
  assert.match(cardSrc, /const panel = reviewPanelSummary\(pendingItems\.length, sentRows\.length\)/)
  assert.match(cardSrc, /const grouped = panel\.count > 1/)
  assert.match(cardSrc, /contrib-card-stack--grouped/)
  assert.match(cardSrc, /\{panel\.title\}/)
  assert.match(cardSrc, /\{panel\.copy\}/)
  assert.match(cardCss, /\.contrib-card-stack\s*\{[\s\S]*?width:\s*min\(100%, 640px\);[\s\S]*?margin-inline:\s*auto;/)
  assert.match(cardCss, /\.contrib-card-stack--grouped\s*\{[\s\S]*?max-height:\s*min\(52vh, 520px\);/)
  assert.match(cardCss, /\.contrib-card-stack--grouped \.contrib-card\s*\{[\s\S]*?border-radius:\s*0;/)
})

test('stack layers collapse into one ordered review item', () => {
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
  assert.equal(items[1].record.id, 'single')
})

test('loaded older backends group canonical stack branches during hot reload', () => {
  const payload = { records: [
    // Deliberately opposite lexical order: branch position, not record id,
    // owns the chain ordering during a frontend-before-backend rollout.
    { id: 'a-second', status: 'prepared', repo: PLATFORM_REPO, is_stack: true,
      branch: 'stack/drawer-navigation/02-pins' },
    { id: 'z-first', status: 'prepared', repo: PLATFORM_REPO, is_stack: true,
      branch: 'stack/drawer-navigation/01-apps' },
  ] }
  const items = reviewItems(payload)
  assert.equal(items.length, 1)
  assert.equal(items[0].kind, 'stack')
  assert.equal(items[0].stack.name, 'Drawer navigation')
  assert.deepEqual(items[0].records.map(record => record.id), ['z-first', 'a-second'])
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
  assert.equal(item.kind, 'stack')
  assert.equal(rememberReviewItemDismissed(item, storage), true)
  assert.equal(visibleReviewItems(payload, storage).length, 0)
  payload.records[0].updated_at = '2'
  assert.equal(visibleReviewItems(payload, storage).length, 1)
})

test('the renderer gives a stack one review-together card, not one card per layer', () => {
  assert.match(cardSrc, /visibleReviewItems\(data, storage\)/)
  assert.match(cardSrc, /item\.kind === 'stack'/)
  assert.match(cardSrc, /<StackReviewRow/)
  assert.match(cardSrc, /Review in Contribute/)
})

test('the grouped panel survives pending-to-contributed transitions', () => {
  assert.deepEqual(reviewPanelSummary(3, 0), {
    count: 3,
    title: '3 reviews ready',
    copy: 'Each item keeps its own review and action.',
  })
  assert.deepEqual(reviewPanelSummary(2, 1), {
    count: 3,
    title: '2 remaining · 1 contributed',
    copy: 'Each item keeps its own review and action.',
  })
  assert.deepEqual(reviewPanelSummary(0, 2), {
    count: 2,
    title: '2 items contributed',
    copy: 'Each item was contributed separately.',
  })
  assert.match(cardSrc, /const \[sentRows, setSentRows\] = useState\(\[\]\)/)
  assert.match(cardSrc, /setSentRows\(rows => \[/)
  assert.match(cardSrc, /\{sentRows\.map\(sent => \(/)
  assert.match(cardSrc, /rows\.filter\(row => row\.id !== sent\.id\)/)
})

test('independent cards can submit in parallel without duplicating one record', () => {
  assert.match(cardSrc, /function ReviewRow\(/)
  assert.match(cardSrc, /const activeSendRef = useRef\(false\)/)
  assert.match(cardSrc, /if \(activeSendRef\.current\) return/)
  assert.match(cardSrc, /activeSendRef\.current = true/)
  assert.match(cardSrc, /const \[busy, setBusy\] = useState\(false\)/)
  assert.match(cardSrc, /disabled=\{busy \|\| submitting\}/)
  assert.doesNotMatch(cardSrc, /busyIds|errorsById/)
  assert.match(cardSrc, /item => item\.kind !== 'record' \|\| !sentIds\.has\(item\.record\.id\)/)
})

test('a grouped panel does not repeat the same audience payoff on every card', () => {
  assert.match(cardSrc, /showPayoff=\{!grouped\}/)
  assert.match(cardSrc, /showPayoff && !error && !submitting/)
})

// The action names the value of contributing, not the mechanism of sending, and
// never uses "upstream" — precise to anyone who works with open source, opaque to
// everyone else. It only names a destination it actually knows.
test('the action label says contribute, and only names Möbius for Möbius', () => {
  assert.equal(contributeLabel({ repo: PLATFORM_REPO }), 'Contribute to Möbius')
  assert.equal(
    contributeLabel({ repo: 'mobius-os/app-example' }),
    'Contribute this improvement',
  )
  assert.equal(contributeLabel(null), 'Contribute this improvement')
  for (const label of [contributeLabel({ repo: PLATFORM_REPO }), contributeLabel({})]) {
    assert.doesNotMatch(label, /upstream|pull request|PR\b/i)
  }
})

// The payoff line motivates without overpromising: acceptance stays the
// maintainers' decision, not a consequence of the tap.
test('the payoff line matches who benefits and keeps acceptance conditional', () => {
  const platform = payoffLine({ repo: PLATFORM_REPO })
  assert.match(platform, /everyone running Möbius/)
  const app = payoffLine({ repo: 'mobius-os/app-example' })
  assert.match(app, /everyone using this app/)
  for (const line of [platform, app]) assert.match(line, /^If it's accepted/)
})

test('the docked card reduces a multi-line diff stat to its aggregate', () => {
  assert.equal(
    diffStatSummary(
      ' frontend/src/a.js | 12 +++++\n backend/app/b.py | 3 ---\n 2 files changed, 12 insertions(+), 3 deletions(-)',
    ),
    '2 files changed, 12 insertions(+), 3 deletions(-)',
  )
  assert.equal(diffStatSummary(' 1 file changed, 4 insertions(+)\n'), '1 file changed, 4 insertions(+)')
  assert.equal(diffStatSummary(null), '')
  assert.match(cardSrc, /diffStatSummary\(record\.diff_stat\)/)
})

test('the card discloses the continuing review authority granted by Send', () => {
  assert.match(cardSrc, /autopilot=\{autopilotOnSend\(data\)\}/)
  assert.match(cardSrc, /Möbius will also handle review feedback/)
})

// The whole safety argument for a one-tap chat button is that it reuses the app's
// verified path. If the card ever grew its own push/PR-creation logic, or dropped
// the provenance tag, that argument would quietly stop being true.
test('Send routes through the same verified submit endpoint as the app button', () => {
  assert.match(clientSrc, /\/github\/contributions\/\$\{appId\}\/\$\{encodeURIComponent\(recordId\)\}\/submit/)
  assert.match(clientSrc, /submitter: 'chat-review-card'/)
  assert.match(cardSrc, /api\.contributions\.submit\(appId, record\.id/)
  // Anchored on real invocations. A bare /gh / also matches ordinary English
  // ("through the", "enough room"), which made this fire on a prose comment.
  const forbidden = [
    /\bgh\s+(?:pr|api|issue|repo)\b/,
    /api\.github\.com/,
    /\/repos\//,
    /git\s+push/,
  ]
  for (const pattern of forbidden) {
    assert.doesNotMatch(cardSrc, pattern,
      'the card must not talk to GitHub itself — the platform endpoint owns that')
  }
})

// A public, irreversible action must show what it will publish. The expander is
// what makes one tap honest rather than blind.
test('the card exposes the exact text and file list that would be published', () => {
  assert.match(cardSrc, /record\.body_draft/)
  assert.match(cardSrc, /record\.files\?\.length > 0/)
  assert.match(cardSrc, /The exact text that will be published/)
})

// `.chat__foot` is a transparent overlay with `pointer-events: none` so the
// transcript scrolls behind the composer; every real control inside it opts back
// in. A card that forgets renders perfectly and ignores every tap — the hit test
// falls through to the messages underneath — which no amount of programmatic
// clicking in a test reveals.
test('the card opts back into pointer events inside the pass-through foot', () => {
  assert.match(
    cardCss,
    /\.contrib-card-stack,\s*\n\.contrib-card \{\s*\n\s*pointer-events: auto;/,
  )
})

// ── Swipe-to-dismiss ────────────────────────────────────────────────────────

function fakeStorage(initial = {}) {
  const map = new Map(Object.entries(initial))
  return {
    getItem: key => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => { map.set(key, String(value)) },
    size: () => map.size,
  }
}

test('only a decisively sideways drag counts, in either direction', () => {
  assert.equal(isHorizontalSwipe(20, 0), true)
  assert.equal(isHorizontalSwipe(-20, 0), true)
  // Vertical and near-diagonal movement belongs to the details scroller.
  assert.equal(isHorizontalSwipe(0, 30), false)
  assert.equal(isHorizontalSwipe(20, 19), false)
  // Below the slop nothing is a gesture yet.
  assert.equal(isHorizontalSwipe(8, 0), false)
})

test('dismissal needs real travel, so a tap or a nudge cannot lose the card', () => {
  assert.equal(passedDismissThreshold(DISMISS_DX_PX, 0), true)
  assert.equal(passedDismissThreshold(-DISMISS_DX_PX, 0), true)
  assert.equal(passedDismissThreshold(DISMISS_DX_PX - 1, 0), false)
  assert.equal(passedDismissThreshold(0, 0), false)
  // Travel far enough but mostly downward: still the scroller's gesture.
  assert.equal(passedDismissThreshold(70, 90), false)
})

// Dismissing is a VIEW decision. The record stays prepared in the ledger, so an
// accidental swipe can never drop staged work — it only stops the card asking.
test('dismissal hides a record without touching the ledger', () => {
  const record = { id: 'r1', status: 'prepared', updated_at: 'T1' }
  const store = fakeStorage()
  const payload = { records: [record] }
  assert.equal(visibleRecords(payload, store).length, 1)
  rememberDismissed(record, store)
  assert.equal(isDismissed(record, store), true)
  assert.equal(visibleRecords(payload, store).length, 0)
  // The record object itself is untouched — nothing about it says "dismissed".
  assert.deepEqual(record, { id: 'r1', status: 'prepared', updated_at: 'T1' })
})

// A dismissal means "not this version". Re-staging is a fresh decision, so the
// card must come back rather than the first swipe burying every later revision.
test('a re-staged record reappears after being dismissed', () => {
  const store = fakeStorage()
  const first = { id: 'r1', status: 'prepared', updated_at: 'T1' }
  rememberDismissed(first, store)
  const restaged = { id: 'r1', status: 'prepared', updated_at: 'T2' }
  assert.equal(isDismissed(restaged, store), false)
  assert.equal(visibleRecords({ records: [restaged] }, store).length, 1)
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
  // A record with no id has no dismissal identity at all.
  assert.equal(dismissKey({ updated_at: 'T1' }), null)
  assert.equal(rememberDismissed({}, fakeStorage()), false)
})

// A touch-only dismissal would be unreachable with a mouse or a keyboard.
test('the swipe has a visible, focusable equivalent', () => {
  assert.match(cardSrc, /className="contrib-card__dismiss"/)
  assert.match(cardSrc, /aria-label="Dismiss — keeps it in Contribute"/)
  assert.match(cardSrc, /onClick=\{\(\) => onDismiss\?\.\(\)\}/)
  // Every card shape shares the shell's OpenAI SDK close icon.
  assert.match(cardSrc, /import \{ X \} from '@openai\/apps-sdk-ui\/components\/Icon'/)
  assert.equal(
    (cardSrc.match(/<X width=\{14\} height=\{14\} aria-hidden="true" \/>/g) || []).length,
    3,
  )
  assert.doesNotMatch(cardSrc, /title="Dismiss/)
  assert.doesNotMatch(cardSrc, /✕|✖|❌/)
})

// Same lesson as the navigation drawer: a passive listener can watch a gesture
// but never claim it, and touch-action cannot cover for that on WebKit.
test('the dismissal gesture is claimed with a non-passive touchmove', () => {
  assert.match(cardSrc, /addEventListener\('touchmove', onMove, \{ passive: false \}\)/)
  assert.match(cardSrc, /event\.preventDefault\(\)/)
  assert.doesNotMatch(cardSrc, /onTouchMove=\{/)
})

// The first version of the acknowledgement had NO exit: no swipe (its handlers
// lived in the other card shape) and no control in a band it never rendered.
// Every card shape must remain explicitly dismissible, while the acknowledgement
// and its interactive GitHub link must never disappear on a timer.
test('the post-send acknowledgement persists until an explicit user or navigation exit', () => {
  const sentRow = cardSrc.slice(
    cardSrc.indexOf('function SentRow('),
    cardSrc.indexOf('function StackReviewRow('),
  )
  assert.ok(sentRow.length > 0, 'the acknowledgement is its own card shape')
  // Same swipe binding as every other card, not a bespoke layout.
  assert.match(sentRow, /useSwipeToDismiss\(onDismiss\)/)
  // A band with a real dismiss control.
  assert.match(sentRow, /className="contrib-card__badge"/)
  assert.match(sentRow, /className="contrib-card__dismiss"/)
  // Interactive content stays until one of those user exits or component
  // navigation owns its lifecycle; elapsed time never removes it.
  assert.doesNotMatch(sentRow, /setTimeout|setInterval/)
  assert.doesNotMatch(cardSrc, /SENT_VISIBLE_MS/)
  assert.match(sentRow, /View on GitHub/)
})

// One gesture implementation for every card shape here. Two copies is how the
// acknowledgement ended up without one.
test('every card shape shares one swipe implementation', () => {
  assert.equal((cardSrc.match(/function useSwipeToDismiss\(/g) || []).length, 1)
  assert.equal((cardSrc.match(/= useSwipeToDismiss\(onDismiss\)/g) || []).length, 3)
  assert.equal((cardSrc.match(/addEventListener\('touchmove'/g) || []).length, 1)
})

test('a failed send is shown only on the record that failed', () => {
  assert.match(cardSrc, /const \[error, setError\] = useState\(null\)/)
  assert.match(cardSrc, /setError\(outcome\.error\)/)
})
