import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
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
  assert.match(html, /Condition owner<\/dt><dd>Hosted deployment/)
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

test('Goal completion and cancellation without a receipt trail the owning answer', async () => {
  const previousWindow = globalThis.window
  globalThis.window = { location: { href: 'http://localhost/' } }
  after(() => { globalThis.window = previousWindow })
  const { default: MsgContent } = await vite.ssrLoadModule(
    '/src/components/ChatView/MsgContent.jsx',
  )
  for (const [status, result_disposition, label] of [
    ['met', 'retired_by_goal_outcome', 'Wait completed'],
    ['cancelled', 'withdrawn', 'Wait stopped'],
    ['met', 'withdrawn', 'Wait completed'],
    ['met', 'withdrawn_after_attempt', 'Wait completed'],
  ]) {
    const summary = {
      id: result_disposition, description: 'Exact Goal result', status,
      result_disposition, delivery_pending: false,
    }
    assert.equal(waitWokeItsAnswer(summary), false)
    const html = renderToStaticMarkup(createElement(MsgContent, {
      msg: { id: 'owning-answer', role: 'assistant', content: 'Goal closed.', wait_summaries: [summary] },
    }))
    assert.ok(html.indexOf('Goal closed.') < html.indexOf(label), `${result_disposition} must not start an answer`)
    assert.doesNotMatch(html, /chat__answer-cause/)
  }
})

test('normal delivery wakes an answer; legacy and activation receipts keep their placement', async () => {
  const previousWindow = globalThis.window
  globalThis.window = { location: { href: 'http://localhost/' } }
  after(() => { globalThis.window = previousWindow })
  const { default: MsgContent } = await vite.ssrLoadModule(
    '/src/components/ChatView/MsgContent.jsx',
  )
  const base = { description: 'Exact Goal result', status: 'met', delivery_pending: false }
  for (const result_disposition of ['delivered', 'legacy_consumed', 'scheduled', null]) {
    const summary = { ...base, id: String(result_disposition), result_disposition }
    assert.equal(waitWokeItsAnswer(summary), true, String(result_disposition))
    const html = renderToStaticMarkup(createElement(MsgContent, {
      msg: { id: 'receiving-answer', role: 'assistant', content: 'Follow-up begins.', wait_summaries: [summary] },
    }))
    assert.ok(html.indexOf('Wait completed') < html.indexOf('Follow-up begins.'), String(result_disposition))
  }
  assert.equal(waitWokeItsAnswer(base), true, 'old summaries omit the disposition')
  assert.equal(waitWokeItsAnswer({ ...base, result_disposition: 'legacy_consumed', delivery_pending: true }), false)
  assert.equal(waitWokeItsAnswer({ ...base, status: 'cancelled', result_disposition: null }), false)
})

test('owed ordinary results expose Stop follow-up even behind a recovery hold', () => {
  for (const status of ['met', 'expired', 'failed']) {
    for (const resume_blocker of [null, 'owner_input', 'manual_resume', 'resume_failed', 'restart']) {
      for (const expanded of [false, true]) {
        const html = renderToStaticMarkup(createElement(WaitCard, {
          wait: { id: 'owed-result', kind: 'timer', status, delivery_pending: true, resume_blocker, can_withdraw: true },
          expanded, onToggle: () => {}, onCancel: () => {}, onRevealRecovery: () => {},
        }))
        // Stopping is chosen from the details; the collapsed line stays one row.
        if (expanded) assert.match(html, /Stop follow-up/, `${status}: ${resume_blocker}`)
        else assert.doesNotMatch(html, /Stop follow-up/, `${status}: ${resume_blocker}`)
        assert.doesNotMatch(html, /Stop waiting/)
        if (['manual_resume', 'resume_failed', 'restart'].includes(resume_blocker)) {
          assert.match(html, /View recovery/, 'withdrawal must not replace recovery access')
        }
      }
    }
  }
})

test('delivered, withdrawn and activation waits never expose a generic Stop control', () => {
  for (const wait of [
    { kind: 'timer', status: 'met', delivery_pending: false, result_disposition: 'delivered' },
    { kind: 'timer', status: 'met', delivery_pending: false, result_disposition: 'withdrawn' },
    { kind: 'timer', status: 'met', delivery_pending: false, result_disposition: 'withdrawn_after_attempt' },
    { kind: 'timer', status: 'met', delivery_pending: true, can_withdraw: false },
    { kind: 'timer', status: 'cancelled', delivery_pending: false },
    { kind: 'platform_activation', status: 'armed' },
    { kind: 'platform_activation', status: 'met', delivery_pending: true },
  ]) {
    const html = renderToStaticMarkup(createElement(WaitCard, {
      wait: { id: 'settled', ...wait }, expanded: true, onToggle: () => {}, onCancel: () => {},
    }))
    assert.doesNotMatch(html, /Stop follow-up|Stop waiting/)
  }
})


test('ended-attempt withdrawal remains a non-waking historical fact, not provider delivery', () => {
  for (const status of ['met', 'expired', 'failed']) {
    assert.equal(waitWokeItsAnswer({ status, delivery_pending: false,
      result_disposition: 'withdrawn_after_attempt' }), false)
  }
})


test('an unresolved admitting run hides Stop follow-up without hiding recovery', () => {
  const html = renderToStaticMarkup(createElement(WaitCard, {
    wait: { id: 'unresolved-attempt', kind: 'timer', status: 'met', delivery_pending: true,
      can_withdraw: false, resume_blocker: 'manual_resume' },
    expanded: true, onToggle: () => {}, onCancel: () => {}, onRevealRecovery: () => {},
  }))
  assert.doesNotMatch(html, /Stop follow-up|Stop waiting/)
  assert.match(html, /View recovery/)
})
