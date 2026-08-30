import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  cacheHitRate,
  formatCacheHitRate,
  formatUsageMenuText,
  nonCachedInputTokens,
  usageModelName,
} from '../chatUsageFormat.js'

test('Brain usage summary leads with input, output, cache hit, and cost', () => {
  const totals = {
    cost_usd: 1.234,
    input_tokens: 4_300_000,
    cache_read_input_tokens: 4_000_000,
    output_tokens: 20_000,
    total_tokens: 4_320_000,
  }

  assert.equal(
    formatUsageMenuText(totals),
    '300k in · 20k out · 93% cache · $1.23',
  )
  assert.doesNotMatch(formatUsageMenuText(totals), /4\.3M/)
})

test('Brain usage summary remains useful when cost is unavailable', () => {
  assert.equal(formatUsageMenuText({
    input_tokens: 84_210,
    cache_read_input_tokens: 0,
    output_tokens: 1_210,
  }), '84k in · 1.2k out · 0% cache')
  assert.equal(formatUsageMenuText({}), null)
})

test('usage helpers distinguish non-cached input from cumulative input', () => {
  const totals = {
    input_tokens: 15_508_736,
    cache_read_input_tokens: 15_016_960,
  }
  assert.equal(nonCachedInputTokens(totals), 491_776)
  assert.equal(cacheHitRate(totals), 96.82903880754692)
  assert.equal(formatCacheHitRate(totals), '97%')
  assert.equal(formatCacheHitRate(totals, 1), '96.8%')
})

test('usage model name is provider-neutral', () => {
  assert.equal(usageModelName({ model: 'gpt-5.6-sol' }), 'gpt-5.6-sol')
  assert.equal(usageModelName({
    provider_model_usage: { 'claude-opus-4-8': { inputTokens: 1 } },
  }), 'claude-opus-4-8')
  assert.equal(usageModelName({}), null)
})
