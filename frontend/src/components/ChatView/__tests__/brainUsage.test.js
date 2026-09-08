import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  contextTokenCounts,
  contextUsedPercent,
  formatRoundedTokenCount,
  modelContextTokenCounts,
  resolvedContextTokenCounts,
} from '../brainUsage.js'
import { chatQueries } from '../../../hooks/queries.js'

const chatViewSource = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const brainIconSource = readFileSync(new URL('../BrainUsageIcon.jsx', import.meta.url), 'utf8')

test('context gauge measures the latest model call against its context window', () => {
  assert.equal(contextUsedPercent({
    input_tokens: 193_800,
    context_window: 258_400,
  }), 75)
  assert.equal(contextUsedPercent({ input_tokens: 300, context_window: 200 }), 100)
  assert.equal(contextUsedPercent({ input_tokens: null, context_window: 200 }), null)
  assert.equal(contextUsedPercent({ input_tokens: 100, context_window: 0 }), null)
})

test('context legend keeps token counts to three digits and a unit symbol', () => {
  assert.deepEqual(contextTokenCounts({
    input_tokens: 44_063,
    context_window: 258_400,
  }), { used: 44_063, maximum: 258_400 })
  assert.equal(formatRoundedTokenCount(66_648), '67k')
  assert.equal(formatRoundedTokenCount(258_400), '258k')
  // Step up a unit instead of spilling into four-plus digits with a comma.
  assert.equal(formatRoundedTokenCount(1_000_000), '1M')
  assert.equal(formatRoundedTokenCount(1_400_000), '1.4M')
  assert.equal(formatRoundedTokenCount(1_030_000_000), '1G')
  // A value just under a threshold rounds up into the next unit rather than
  // spilling to four digits (999_999 → "1M", not "1000k").
  assert.equal(formatRoundedTokenCount(999_999), '1M')
  assert.equal(formatRoundedTokenCount(999_999_999), '1G')
  assert.equal(formatRoundedTokenCount(0), '0')
  assert.equal(contextTokenCounts({ input_tokens: null, context_window: 258_400 }), null)
})

test('a new chat starts at zero against the selected model context', () => {
  const registry = {
    codex: [
      { id: 'gpt-5.6-sol', context_window: 258_400 },
      { id: 'gpt-5.3-codex-spark', context_window: 121_600 },
    ],
  }
  assert.deepEqual(
    modelContextTokenCounts(registry, 'codex', 'gpt-5.6-sol'),
    { used: 0, maximum: 258_400 },
  )
  assert.equal(modelContextTokenCounts(registry, 'codex', 'missing'), null)
  assert.deepEqual(resolvedContextTokenCounts({
    provider: 'codex',
    provider_session_id: null,
    input_tokens: null,
    context_window: null,
  }, registry, 'codex', 'gpt-5.6-sol'), {
    used: 0,
    maximum: 258_400,
  })
})

test('missing usage in an established session remains unknown', () => {
  const registry = {
    codex: [{ id: 'gpt-5.6-sol', context_window: 258_400 }],
  }
  assert.equal(resolvedContextTokenCounts({
    provider: 'codex',
    provider_session_id: 'session-without-usage',
    input_tokens: null,
    context_window: null,
  }, registry, 'codex', 'gpt-5.6-sol'), null)
  assert.equal(
    resolvedContextTokenCounts(null, registry, 'codex', 'gpt-5.6-sol'),
    null,
  )
  assert.equal(resolvedContextTokenCounts({
    provider: 'claude',
    provider_session_id: null,
    input_tokens: null,
    context_window: null,
  }, registry, 'codex', 'gpt-5.6-sol'), null)
})

test('context usage cache identity follows the exact provider session', () => {
  const first = chatQueries.keys.currentUsage('chat-1', 'codex', 'session-1')
  const second = chatQueries.keys.currentUsage('chat-1', 'codex', 'session-2')
  assert.deepEqual(first, [
    'chat-current-usage', 'chat-1', 'codex', 'session-1',
  ])
  assert.notDeepEqual(first, second)
})

test('the brain reads the selected model through the durable chat policy', () => {
  assert.match(chatViewSource, /model=\{selectedChatModel\(chatInfo\)\}/)
  assert.doesNotMatch(
    chatViewSource,
    /model=\{chatInfo\?\.effective_agent_settings\?\.model\}/,
  )
})

test('a settled turn publishes its new provider session to the open pane', () => {
  assert.match(
    chatViewSource,
    /const refreshedChatInfo = chatDetailCacheValue\(data\)\.chatInfo/,
  )
  assert.match(
    chatViewSource,
    /updateChatRuntimeCache\([\s\S]*?chatInfo: refreshedChatInfo/,
  )
})

test('the accepted brain treatment keeps both fills inside a 38px circular trigger', () => {
  assert.match(brainIconSource, /const DEFAULT_SIZE = 38/)
  assert.match(brainIconSource, /const OUTLINE_EROSION_RADIUS = 0\.25/)
  assert.match(brainIconSource, /const FILL_INSET = NOMINAL_BOUNDARY_WIDTH - OUTLINE_EROSION_RADIUS/)
  assert.match(brainIconSource, /const VISIBLE_TOP = TOP \+ FILL_INSET/)
  assert.match(brainIconSource, /const VISIBLE_BOTTOM = BOTTOM - FILL_INSET/)
  assert.match(brainIconSource, /const fillHeight = \(\(VISIBLE_BOTTOM - VISIBLE_TOP\) \* clamped\) \/ 100/)
  assert.match(brainIconSource, /<mask[\s\S]*?id=\{fillMaskId\}[\s\S]*?maskUnits="userSpaceOnUse"/)
  assert.match(brainIconSource, /strokeWidth=\{FILL_INSET \* 2\}/)
  assert.match(brainIconSource, /<g mask=\{`url\(#\$\{fillMaskId\}\)`\}>/)
  assert.doesNotMatch(brainIconSource, /clipPath=\{`url\(#\$\{silhouetteId\}\)`\}/)
})
