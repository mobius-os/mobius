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
const { waitHistoryViewModel } = await vite.ssrLoadModule(
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

test('a wait that woke the chat leads its answer while a deliberate stop trails it', async () => {
  const { waitHistoryPlacement } = await vite.ssrLoadModule(
    '/src/components/ChatView/waitHistory.js',
  )
  assert.deepEqual(
    ['met', 'expired', 'failed', 'cancelled'].map(status => waitHistoryPlacement({ status })),
    ['lead', 'lead', 'lead', 'trail'],
  )
})

test('the wake cause is visible at the top of the answer while it is still streaming', async () => {
  const previousWindow = globalThis.window
  globalThis.window = { location: { href: 'http://localhost/' } }
  after(() => { globalThis.window = previousWindow })
  const { default: MsgContent } = await vite.ssrLoadModule(
    '/src/components/ChatView/MsgContent.jsx',
  )
  const msg = {
    id: 'wait-resume-sample',
    role: 'assistant',
    content: 'The runner file is clean now, so I will continue.',
    wait_summaries: [
      { id: 'woke', description: 'Runner edits committed', status: 'met', checks_count: 2 },
      { id: 'stopped', description: 'Obsolete deploy check', status: 'cancelled' },
    ],
  }
  const live = renderToStaticMarkup(createElement(MsgContent, { msg, isStreaming: true }))
  assert.match(live, /Wait completed/)
  assert.ok(live.indexOf('Wait completed') < live.indexOf('runner file is clean'))
  assert.doesNotMatch(live, /Wait stopped/)

  const settled = renderToStaticMarkup(createElement(MsgContent, { msg, isStreaming: false }))
  assert.ok(settled.indexOf('runner file is clean') < settled.indexOf('Wait stopped'))
})
