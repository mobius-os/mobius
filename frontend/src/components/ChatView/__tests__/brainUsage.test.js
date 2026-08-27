import test from 'node:test'
import assert from 'node:assert/strict'

import {
  contextTokenCounts,
  contextUsedPercent,
  formatRoundedTokenCount,
  modelContextTokenCounts,
  resolvedContextTokenCounts,
  visibleBrainFillBounds,
} from '../brainUsage.js'

test('context gauge measures the latest model call against its context window', () => {
  assert.equal(contextUsedPercent({
    input_tokens: 193_800,
    context_window: 258_400,
  }), 75)
  assert.equal(contextUsedPercent({ input_tokens: 300, context_window: 200 }), 100)
  assert.equal(contextUsedPercent({ input_tokens: null, context_window: 200 }), null)
  assert.equal(contextUsedPercent({ input_tokens: 100, context_window: 0 }), null)
})

test('context legend rounds current and maximum token counts to 1k', () => {
  assert.deepEqual(contextTokenCounts({
    input_tokens: 44_063,
    context_window: 258_400,
  }), { used: 44_063, maximum: 258_400 })
  assert.equal(formatRoundedTokenCount(66_648), '67k')
  assert.equal(formatRoundedTokenCount(258_400), '258k')
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

test('the final ten percent remains visibly linear inside the inset mask', () => {
  const geometry = percent => visibleBrainFillBounds(percent, {
    top: 2.3,
    bottom: 21.7,
    inset: 1.75,
  })
  const at90 = geometry(90)
  const at95 = geometry(95)
  const at100 = geometry(100)

  assert.ok(at95.fillHeight > at90.fillHeight)
  assert.ok(at100.fillHeight > at95.fillHeight)
  assert.ok(Math.abs(
    (at95.fillHeight - at90.fillHeight)
    - (at100.fillHeight - at95.fillHeight),
  ) < 1e-12)
  assert.ok(Math.abs(at100.fillY - 4.05) < 1e-12)
  assert.deepEqual(geometry(105), at100)
  assert.equal(geometry(-5).fillHeight, 0)
})
