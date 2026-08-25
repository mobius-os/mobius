import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  formatUsageAriaSummary,
  formatUsageStripText,
  usageModelName,
} from '../chatUsageFormat.js'

test('usage strip names provider-reported cost without repeating totals', () => {
  const totals = {
    cost_usd: 1.234,
    input_tokens: 4_300_000,
    output_tokens: 20_000,
    total_tokens: 4_320_000,
  }

  assert.equal(
    formatUsageStripText(totals),
    'Usage · $1.23 · 4.3M in / 20k out',
  )
  assert.doesNotMatch(formatUsageStripText(totals), /4\.3M tokens/)
  assert.match(formatUsageAriaSummary(totals), /reported cost \$1\.23/)
})

test('usage model name is provider-neutral', () => {
  assert.equal(usageModelName({ model: 'gpt-5.6-sol' }), 'gpt-5.6-sol')
  assert.equal(usageModelName({
    provider_model_usage: { 'claude-opus-4-8': { inputTokens: 1 } },
  }), 'claude-opus-4-8')
  assert.equal(usageModelName({}), null)
})
