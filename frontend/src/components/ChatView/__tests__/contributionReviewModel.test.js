import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  autopilotOnSend,
  contributeApp,
  contributeAppId,
  contributionReviewIntent,
  currentReviewItems,
  publicationAction,
  publicationFailureOwner,
  publicationItemsAction,
  publicationStackAction,
  reviewActionKey,
  reviewItems,
  sendBlocker,
  stackSendBlocker,
} from '../contributionReviewModel.js'

const changesSrc = readFileSync(new URL('../ChatDiffViewer.jsx', import.meta.url), 'utf8')
const chatViewSrc = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const publicationSrc = readFileSync(new URL('../chatContributionPublication.js', import.meta.url), 'utf8')
const clientSrc = readFileSync(new URL('../../../api/client.js', import.meta.url), 'utf8')
const PLATFORM_REPO = 'mobius-os/mobius'
const APPS = [{ id: 3, slug: 'other' }, { id: 8, slug: 'contribute' }]

test('the ledger owner is resolved by slug', () => {
  assert.equal(contributeAppId(APPS), 8)
  assert.equal(contributeAppId([{ id: 3, slug: 'other' }]), null)
  assert.equal(contributeAppId([]), null)
  assert.equal(contributeAppId(undefined), null)
  assert.equal(contributeApp(APPS, 8).slug, 'contribute')
  assert.equal(contributeApp(APPS, 99), null)
})

test('every unsettled review remains reachable in Changes', () => {
  const payload = { records: [
    { id: 'ready', status: 'prepared', review: { state: 'ready' } },
    { id: 'drifted', status: 'prepared', review: { state: 'needs_refresh' } },
    { id: 'unreviewed', status: 'prepared' },
    { id: 'publishing', status: 'submitting' },
  ] }
  assert.deepEqual(reviewItems(payload).map(item => item.id), [
    'ready', 'drifted', 'unreviewed', 'publishing',
  ])
})

test('record identities become opaque review intents, never routes', () => {
  assert.equal(contributionReviewIntent({ id: 'review.1-ready' }), 'review:review.1-ready')
  assert.equal(contributionReviewIntent({ id: '  review_2  ' }), 'review:review_2')
  assert.equal(contributionReviewIntent({ id: '../escape' }), null)
  assert.equal(contributionReviewIntent({ id: 'has spaces' }), null)
  assert.equal(contributionReviewIntent(null), null)
})

test('direct send requires the exact reviewed happy path', () => {
  const ready = {
    id: 'ready', status: 'prepared', quality_review_ready: true,
    review: { state: 'ready' },
  }
  assert.equal(sendBlocker(ready, { connected: true }), null)
  assert.match(sendBlocker({ ...ready, quality_review_ready: false }), /exact agent review/)
  assert.match(sendBlocker({ ...ready, review: { state: 'needs_refresh', message: 'Moved' } }), /Moved/)
  assert.match(sendBlocker({ ...ready, last_submit_error: 'failed' }), /fresh check/)
  assert.match(sendBlocker({ ...ready, is_stack: true }), /linked set/)
  assert.match(sendBlocker(ready, { connected: false }), /Connect GitHub/)
  assert.match(sendBlocker({ ...ready, status: 'submitting' }), /still being confirmed/)
  assert.deepEqual(publicationAction(ready), { label: 'Send PR', busyLabel: 'Sending PR' })
  assert.deepEqual(publicationAction({ ...ready, action: 'pr_update' }), {
    label: 'Update PR', busyLabel: 'Updating PR',
  })
  assert.equal(autopilotOnSend({ autopilot_available: true }), true)
  assert.equal(autopilotOnSend({ autopilot_available: true, autopilot_default: false }), false)
  assert.equal(autopilotOnSend({ autopilot_available: false }), false)
})

test('agent contribution intents start one attached worker without running the source chat', () => {
  assert.match(
    chatViewSrc,
    /const overview = await refreshContributionOverview\(appId\)[\s\S]*?overview && !chatChangesActionIsCurrent\(overview, action\)[\s\S]*?api\.contributions\.startWork\(appId, chatId, request\)/,
  )
  const workerStart = chatViewSrc.slice(
    chatViewSrc.indexOf('const startContributionWork'),
    chatViewSrc.indexOf('const handlePrepareChatChanges'),
  )
  assert.doesNotMatch(workerStart, /doSend\(/)
  assert.doesNotMatch(workerStart, /contributeAppId\(builtApps\)/)
  assert.match(workerStart, /const appId = Number\(context\?\.appId\)/)
  assert.match(workerStart, /contributionIntentClaimsRef\.current\.has\(key\)/)
  assert.match(workerStart, /setQueryData\([\s\S]*work: payload\.work/)
  assert.match(clientSrc, /for-chat\/\$\{encodeURIComponent\(chatId\)\}\/work/)
  assert.match(chatViewSrc, /kind: 'unsorted', revision/)
  assert.match(chatViewSrc, /kind: 'workflow', revision/)
  assert.match(chatViewSrc, /kind: 'records', recordKeys/)
})

test('stack layers collapse into one ordered Changes item', () => {
  const payload = { records: [
    { id: 'three', status: 'prepared', repo: PLATFORM_REPO,
      stack: { id: 'drawer', name: 'Drawer navigation', position: 3, total: 3 } },
    { id: 'single', status: 'prepared' },
    { id: 'one', status: 'prepared', repo: PLATFORM_REPO,
      stack: { id: 'drawer', name: 'Drawer navigation', position: 1, total: 3 } },
    { id: 'two', status: 'prepared', repo: PLATFORM_REPO,
      stack: { id: 'drawer', name: 'Drawer navigation', position: 2, total: 3 } },
  ] }
  const items = reviewItems(payload)
  assert.equal(items.length, 2)
  assert.equal(items[0].kind, 'stack')
  assert.deepEqual(items[0].records.map(record => record.id), ['one', 'two', 'three'])
  assert.equal(items[1].record.id, 'single')
})

test('a complete stack from another source chat becomes one direct approval', () => {
  const layer = (id, position) => ({
    id, status: 'prepared', action: 'pr', quality_review_ready: true,
    review: { state: 'ready' }, repo: PLATFORM_REPO,
    stack: { id: 'approval', name: 'Direct approval', position, total: 2 },
  })
  const first = layer('first', 1)
  const second = layer('second', 2)
  const items = reviewItems({
    records: [second],
    stack_units: [{ id: 'approval', name: 'Direct approval', repo: PLATFORM_REPO,
      records: [first, second] }],
  })
  assert.equal(items.length, 1)
  assert.deepEqual(items[0].records.map(record => record.id), ['first', 'second'])
  assert.equal(stackSendBlocker(items[0], { connected: true }), null)
  assert.deepEqual(publicationStackAction(items[0]), {
    label: 'Send stack', confirmLabel: 'Send PRs', count: 2, updating: false,
  })
  assert.match(stackSendBlocker({ ...items[0], records: [second] }, { connected: true }), /complete linked set/)

  const partial = reviewItems({
    records: [{ ...first, status: 'draft' }],
    stack_units: [{ id: 'approval', name: 'Direct approval', repo: PLATFORM_REPO,
      records: [{ ...first, status: 'draft', review: null }, second] }],
  })
  assert.equal(stackSendBlocker(partial[0], { connected: true }), null)
  assert.equal(publicationStackAction(partial[0]).count, 1)
})

test('confirmation copy counts exact pull requests rather than stack containers', () => {
  const stack = (id, action = 'pr') => ({
    kind: 'stack', id,
    records: [1, 2].map(position => ({ id: `${id}-${position}`, status: 'prepared', action })),
  })
  assert.deepEqual(publicationItemsAction([stack('one'), stack('two')]), {
    count: 4, updating: false,
    promptLabel: 'Send 4 reviewed pull requests?', confirmLabel: 'Send 4 PRs',
  })
  assert.deepEqual(publicationItemsAction([stack('one', 'pr_update')]), {
    count: 2, updating: true,
    promptLabel: 'Update 2 reviewed pull requests?', confirmLabel: 'Update 2 PRs',
  })
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

test('public failures distinguish agent recovery from owner account work', () => {
  assert.equal(publicationFailureOwner({ status: 403, code: 'forbidden' }), 'owner')
  assert.equal(publicationFailureOwner({ status: 0, code: 'unconfirmed_result' }), 'agent')
  assert.equal(publicationFailureOwner({ status: 409, code: 'review_refresh_needed' }), 'agent')
  assert.equal(reviewActionKey({ id: 'one', updated_at: 'T1', status: 'prepared' }), 'one:T1:prepared')
  assert.equal(
    reviewActionKey({ id: 'one', action_key: 'stable', updated_at: 'T2', status: 'open' }),
    'one:stable',
  )
  assert.equal(
    reviewActionKey({ id: 'one', updated_at: 'T1', status: 'open', needs_attention: true }),
    reviewActionKey({ id: 'one', updated_at: 'T2', status: 'open', needs_attention: true }),
  )
})

test('healthy sent records stay quiet while attention remains actionable in Changes', () => {
  assert.deepEqual(reviewItems({ records: [
    { id: 'healthy', status: 'open' },
    { id: 'done', status: 'merged' },
    { id: 'attention', status: 'open', needs_attention: true },
    { id: 'submit-error', status: 'open', last_submit_error: 'Lost response' },
    { id: 'refresh', status: 'open', review: { state: 'needs_refresh' } },
  ] }).map(item => item.id), ['attention', 'submit-error', 'refresh'])
  assert.match(
    chatViewSrc,
    /const revision = reviewActionKey\(record\)[\s\S]*?return startContributionWork\([\s\S]*?`followup:\$\{revision\}`[\s\S]*?followupContributionWork\(record, revision, context\?\.retryOf\)/,
  )
})

test('helper progress and recovery stay inside Changes', () => {
  const startHandlers = chatViewSrc.slice(
    chatViewSrc.indexOf('const handlePrepareChatChanges'),
    chatViewSrc.indexOf('const wasTurnActiveRef'),
  )
  assert.doesNotMatch(startHandlers, /setShowChanges\(false\)/)
  assert.doesNotMatch(chatViewSrc, /ContributionReviewCard|contrib-card-stack/)
  assert.match(changesSrc, /async function requestHelper\(callback\)/)
  assert.match(changesSrc, /await contributionActionOutcome\(callback\)/)
  assert.match(changesSrc, /outcome\.kind === 'unavailable'/)
  assert.match(changesSrc, /outcome\.kind === 'blocked'/)
  assert.doesNotMatch(changesSrc, /requestHelperAndClose/)
  assert.match(
    changesSrc,
    /primaryAction\.kind === 'prepare'[\s\S]*?await requestHelper\([\s\S]*?onPrepareChanges/,
  )
  assert.match(changesSrc, /Changes will stay open so you can keep reviewing\./)
  assert.match(changesSrc, /className=\{`chat-work__helper-request/)
  assert.match(changesSrc, /role=\{helperStartError \? 'alert' : 'status'\}/)
  assert.match(changesSrc, /const retryingStart = state === 'active' && work\?\.status === 'retrying'/)
  assert.match(changesSrc, /retryingStart[\s\S]*?String\(work\?\.result \|\| ''\)\.trim\(\)/)
  assert.match(changesSrc, /It starts after the current reply, then continues in the background/)
  assert.match(changesSrc, /'Stop preparation'/)
  assert.match(clientSrc, /work\/stop/)
  assert.match(chatViewSrc, /const handleStopContributionWork = useCallback/)
  assert.match(changesSrc, /onStop=\{stopHelper\}/)
  assert.match(changesSrc, /work\?\.child_chat_id[\s\S]*?View helper/)
  assert.match(changesSrc, /usage\?\.totals\?\.total_tokens/)
  assert.match(chatViewSrc, /onOpenChat=\{\(childChatId\) => \{/)
  assert.match(changesSrc, /const workContext = contributionWorkContext\(overview\)/)
  assert.match(changesSrc, /workState === 'active' \? null/)
  assert.doesNotMatch(chatViewSrc, /Continue the attached contribution work|handleContinueContributionWork/)
  assert.doesNotMatch(changesSrc, /Continue here|onContinueWork/)
})

test('durable submitting state is visible but never offers a duplicate action', () => {
  assert.match(changesSrc, /const publicationPending = overview\.counts\.submitting > 0/)
  assert.match(changesSrc, /publicationPending \? \(/)
  assert.match(
    changesSrc,
    /<button type="button" className="is-primary" disabled>Confirming…<\/button>/,
  )
})

test('the composer gets unsorted work from the complete chat owner without a persistent card', () => {
  const composerSrc = readFileSync(new URL('../ComposerPopover.jsx', import.meta.url), 'utf8')
  const overviewSrc = readFileSync(new URL('../useChatChangesOverview.js', import.meta.url), 'utf8')
  const querySrc = readFileSync(new URL('../chatChangesQueries.js', import.meta.url), 'utf8')
  assert.match(composerSrc, /useChatChangesOverview\(chatId, initialChangeEntries/)
  assert.match(composerSrc, /changesOverview\.needsAction/)
  assert.match(composerSrc, /Changes need attention/)
  assert.match(overviewSrc, /mergeChatDiffEntries\(diffs\.data \|\| \[\], initialEntries\)/)
  assert.match(querySrc, /loadChatDiffEntries\([\s\S]*?request: apiFetch/)
  assert.match(changesSrc, /onPrepareProject[\s\S]*?&& overview\.lifecycleAvailable/)
})

test('Changes keeps direct publication and review on guarded routes', () => {
  assert.match(clientSrc, /submitter:\s*'chat-review-card'/)
  assert.match(clientSrc, /record\?\.action === 'pr_update'/)
  assert.match(clientSrc, /update-existing/)
  assert.match(publicationSrc, /publish = api\.contributions\.publish/)
  assert.match(clientSrc, /publication_stage: 'ready'/)
  assert.match(changesSrc, /const outcome = await publishContribution\(\{/)
  assert.match(changesSrc, /publicationFailureOwner\(outcome\.failure\) === 'owner'/)
  assert.match(changesSrc, /onContinueInChat\?\.\(record, workContext\)/)
  assert.match(changesSrc, /contributionReviewIntent\(record\)/)
})

test('a public confirmation cannot silently switch to a newer reviewed action', () => {
  const expected = reviewItems({ records: [
    { id: 'one', status: 'prepared', action_key: 'review-a' },
    { id: 'two', status: 'prepared', action_key: 'review-b' },
  ] })
  const unchanged = currentReviewItems(expected, { records: [
    { id: 'one', status: 'prepared', action_key: 'review-a', title: 'Fresh copy' },
    { id: 'two', status: 'prepared', action_key: 'review-b' },
  ] })
  assert.equal(unchanged[0].record.title, 'Fresh copy')
  assert.equal(currentReviewItems(expected, { records: [
    { id: 'one', status: 'prepared', action_key: 'review-c' },
    { id: 'two', status: 'prepared', action_key: 'review-b' },
  ] }), null)
})

test('Changes batch publication refreshes before GitHub and recovers at most once', () => {
  assert.match(changesSrc, /currentReviewItems\(items, refreshed\.data\)/)
  assert.match(
    changesSrc,
    /if \(publishInFlightRef\.current \|\| helperInFlightRef\.current\) return[\s\S]*publishInFlightRef\.current = true/,
  )
  assert.match(changesSrc, /setConfirming\(\[\{ kind: 'record', id: record\.id, record \}\]\)/)
  assert.match(
    changesSrc,
    /const recoveries = outcomes[\s\S]*await requestHelper\([\s\S]*?onContributeAll\?\.\(overview\.workflowRevision, workContext\)[\s\S]*recoveries\.forEach/,
  )
})
