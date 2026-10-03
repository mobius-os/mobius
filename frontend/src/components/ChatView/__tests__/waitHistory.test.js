import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const vite = await createServer({
  appType: 'custom',
  logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
const { default: WaitHistoryCard } = await vite.ssrLoadModule(
  '/src/components/ChatView/WaitHistoryCard.jsx',
)
const { WaitCard } = await vite.ssrLoadModule(
  '/src/components/ChatView/WaitingChip.jsx',
)
const { waitHistoryViewModel, waitWokeItsAnswer } = await vite.ssrLoadModule(
  '/src/components/ChatView/waitHistory.js',
)

after(() => vite.close())

test('Resume when is sentence-cased across active and every settled wait without rewriting identifiers', () => {
  const description = '  resume when GitHub reaches a terminal state for exact peer-network PR #1083 head 018067bdac  '
  const expected = description.trim().replace(/^resume/, 'Resume')
  const wait = { id: 'copy-wait', kind: 'command', description }
  for (const status of ['met', 'expired', 'failed', 'cancelled']) {
    const summary = { ...wait, status }
    assert.equal(waitHistoryViewModel(summary).condition, expected)
    const html = renderToStaticMarkup(createElement(WaitHistoryCard, { summary }))
    assert.ok(html.includes(expected))
    assert.ok(!html.includes('resume when'))
  }
  const html = renderToStaticMarkup(createElement(WaitCard, {
    wait, expanded: true, onToggle: () => {}, onCancel: () => {},
  }))
  assert.ok(html.includes(expected))
  assert.ok(!html.includes('resume when'))
  assert.equal(wait.description, description)
  for (const original of ['gitHub/checks passes', 'Resume when ready', 'resume whenever ready']) {
    assert.equal(waitHistoryViewModel({ description: original, status: 'met' }).condition, original)
  }
})

test('settled waits leave a quiet trace with the full condition', () => {
  const condition = 'Wait until the exact reviewed deployment is serving every replica'
  const common = {
    description: condition,
    condition_owner: 'Hosted deployment',
    checks_count: 3,
    duration_seconds: 125,
  }
  assert.deepEqual(
    ['met', 'expired', 'failed', 'cancelled'].map(status => (
      waitHistoryViewModel({ ...common, status }).kicker
    )),
    [
      'Wait completed',
      'Wait reached its deadline',
      'Wait check failed',
      'Wait stopped',
    ],
  )

  const html = renderToStaticMarkup(createElement(WaitHistoryCard, {
    summary: { ...common, id: 'settled-wait', status: 'met' },
  }))
  assert.match(html, /Wait completed/)
  assert.match(html, /exact reviewed deployment is serving every replica/)
  assert.match(html, /Hosted deployment · 3 checks · 2m 5s/)
})


test('active wait details show the full condition and its owner separately', () => {
  const condition = 'Wait until every replica serves the exact reviewed deployment without truncating this condition'
  const html = renderToStaticMarkup(createElement(WaitCard, {
    wait: {
      id: 'active-wait',
      kind: 'condition',
      description: condition,
      condition_owner: 'Hosted deployment',
      interval_secs: 300,
      checks_count: 2,
      deadline_at: '2026-09-05T01:00:00',
    },
    expanded: true,
    onToggle: () => {},
    onCancel: () => {},
  }))

  assert.match(html, new RegExp(condition))
  assert.match(html, /Handled by<\/dt><dd>Hosted deployment/)
  assert.match(html, /Stop waiting/)
})


test('a Restart wait has no fake deadline or generic cancellation control', () => {
  const html = renderToStaticMarkup(createElement(WaitCard, {
    wait: {
      id: 'activation-wait',
      kind: 'platform_activation',
      description: 'Load committed changes',
      condition_owner: 'Möbius startup',
      interval_secs: 60,
      deadline_at: '2026-09-19T01:00:00',
    },
    expanded: true,
    onToggle: () => {},
    onCancel: () => {},
  }))

  assert.match(html, /Wake-up/)
  assert.match(html, /Restart card has no time limit/)
  assert.doesNotMatch(html, /If it takes too long/)
  assert.doesNotMatch(html, /Stop waiting/)
  assert.doesNotMatch(html, /Sep/)
})

test('what started an answer leads it while live; a stopped wait trails the settled answer', async () => {
  const previousWindow = globalThis.window
  globalThis.window = { location: { href: 'http://localhost/' } }
  after(() => { globalThis.window = previousWindow })
  const { default: MsgContent } = await vite.ssrLoadModule(
    '/src/components/ChatView/MsgContent.jsx',
  )
  const msg = {
    id: 'auto-retry-sample',
    role: 'assistant',
    content: 'Picking up the interrupted work.',
    continuation_reason: 'restart',
    wait_summaries: [
      ...['met', 'expired', 'failed'].map(status => ({
        id: status, description: `Check ${status}`, status,
      })),
      { id: 'stopped', description: 'Obsolete deploy check', status: 'cancelled' },
    ],
  }
  const live = renderToStaticMarkup(createElement(MsgContent, { msg, isStreaming: true }))
  const answer = live.indexOf('Picking up')
  for (const cause of ['Server restarted', 'Wait completed', 'Wait reached its deadline', 'Wait check failed']) {
    assert.ok(live.indexOf(cause) >= 0 && live.indexOf(cause) < answer, cause)
  }
  assert.doesNotMatch(live, /Wait stopped/)

  const settled = renderToStaticMarkup(createElement(MsgContent, { msg, isStreaming: false }))
  assert.ok(settled.indexOf('Picking up') < settled.indexOf('Wait stopped'))
})


test('finished-but-undelivered checks show the blocker, not expired polling promises', () => {
  for (const [blocker, message] of [
    ['platform_restart', 'waiting for platform restart'],
    ['owner_input', 'waiting for your answer'],
    ['manual_resume', 'waiting for Resume'],
    ['provider_park', 'waiting for agent availability'],
    ['restoring_edits', 'restoring local work'],
    ['live_turn', 'waiting for current turn'],
    ['resume_failed', 'follow-up needs attention'],
  ]) {
    const wait = {
      id: 'owed-result', kind: 'command', status: 'met',
      description: 'CI for exact reviewed head', delivery_pending: true,
      resume_blocker: blocker, checks_count: 1, interval_secs: 60,
      deadline_at: '2026-09-01T01:00:00', next_check_at: '2026-09-01T00:00:00',
    }
    const html = renderToStaticMarkup(createElement(WaitCard, {
      wait, expanded: true, onToggle: () => {}, onCancel: () => {},
    }))
    assert.ok(html.includes(`Checks finished; ${message}`))
    assert.match(html, /Original condition<\/dt><dd>CI for exact reviewed head/)
    assert.match(html, /Finished · no more checks/)
    assert.doesNotMatch(html, /next check|wakes to investigate at|Stop waiting/)
  }
})

test('failed and deadline outcomes remain honest while their follow-up is blocked', () => {
  for (const [status, expected] of [
    ['failed', 'Check failed; waiting for platform restart'],
    ['expired', 'Check reached its deadline; waiting for platform restart'],
  ]) {
    const html = renderToStaticMarkup(createElement(WaitCard, {
      wait: { id: status, status, kind: 'command', description: 'CI', delivery_pending: true, resume_blocker: 'platform_restart' },
      expanded: true, onToggle: () => {},
    }))
    assert.ok(html.includes(expected))
    assert.doesNotMatch(html, /Checks finished/)
  }
})

test('history distinguishes a saved result from the continuation it has not woken', () => {
  const summary = { id: 'owed', description: 'Exact CI', status: 'met', delivery_pending: true }
  assert.equal(waitWokeItsAnswer(summary), false)
  assert.equal(waitHistoryViewModel(summary).kicker, 'Condition met · follow-up pending')
  const html = renderToStaticMarkup(createElement(WaitHistoryCard, { summary }))
  assert.doesNotMatch(html, /Wait completed/)
  assert.match(html, /follow-up pending/)
  assert.equal(waitWokeItsAnswer({ ...summary, delivery_pending: false }), true)
})


test('wait explanations show real observations and links while commands stay in opt-in details', () => {
  const html = renderToStaticMarkup(createElement(WaitCard, {
    wait: { id: 'github', kind: 'github_checks', description: 'Checks finish',
      check_description: 'All checks for the reviewed change',
      on_ready: 'Review the results before continuing',
      owner_chat: { id: 'owner-chat', title: 'Reviewing the change' },
      command: 'python3 exact-check.py',
      check_url: 'https://github.com/owner/repo/pull/7/checks',
      latest_result: { state: 'pending', summary: '3 of 4 checks have finished.' },
      checks_count: 2, last_exit_code: 0 },
    expanded: true, onToggle() {}, onCancel() {},
  }))
  assert.match(html, /Checking<\/dt><dd>All checks for the reviewed change/)
  assert.match(html, /Latest result<\/dt><dd>3 of 4 checks have finished/)
  assert.match(html, /Then<\/dt><dd>Review the results before continuing/)
  assert.match(html, /href="\/shell\/\?chat=owner-chat">Reviewing the change/)
  assert.match(html, /<details class="chat__wait-technical"><summary>Technical details<\/summary>/)
  assert.match(html, /<code>python3 exact-check.py<\/code>/)
  assert.match(html, /View GitHub checks/)
  assert.ok(!html.includes('<details open'))
})
