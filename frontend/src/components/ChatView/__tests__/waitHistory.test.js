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
