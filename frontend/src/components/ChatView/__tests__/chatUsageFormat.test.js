import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  cacheHitRate,
  formatCacheHitRate,
  formatTokenCount,
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

test('compact usage distinguishes observed values from complete coverage', () => {
  const totals = { input_tokens: 100, cache_read_input_tokens: 0, output_tokens: 5 }
  assert.match(formatUsageMenuText(totals, {
    runs: 2, runs_by_quality: { final: 1, partial: 1 },
  }), /Reported/)
  assert.match(formatUsageMenuText(totals, {
    runs: 2, runs_by_quality: { final: 1, missing: 1 },
  }), /Reported/)
  assert.doesNotMatch(formatUsageMenuText(totals, {
    runs: 1, runs_by_quality: { final: 1 },
  }), /Reported/)
  assert.equal(formatUsageMenuText({}, {
    runs: 1, runs_by_quality: { unknown: 1 },
  }), 'Usage unknown')
  assert.equal(formatUsageMenuText({ total_tokens: 0 }, {
    runs: 1, runs_by_quality: { partial: 1 },
  }), 'Reported: 0 tokens')
  // A server that predates quality labels makes no completeness claim.
  assert.doesNotMatch(formatUsageMenuText(totals, {
    runs: 2, runs_with_usage: 2,
  }), /Reported/)
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

test('usage model names resolve through the provider-owned registry', () => {
  const registry = {
    codex: [{ id: 'gpt-5.6-sol', label: 'GPT-5.6-Sol' }],
    claude: [{ id: 'claude-opus-4-8', label: 'Claude Opus 4.8' }],
  }
  assert.equal(
    usageModelName({ model: 'gpt-5.6-sol' }, registry, 'codex'),
    'GPT-5.6-Sol',
  )
  assert.equal(usageModelName({
    provider_model_usage: {
      'claude-opus-4-8': { inputTokens: 1 },
      'not-yet-catalogued': { inputTokens: 1 },
    },
  }, registry, 'claude'), 'Claude Opus 4.8, not-yet-catalogued')
})


test('token counts move to the next unit when rounding reaches it', () => {
  assert.equal(formatTokenCount(999), '999')
  assert.equal(formatTokenCount(9_940), '9.9k')
  assert.equal(formatTokenCount(9_960), '10k')
  assert.equal(formatTokenCount(999_400), '999k')
  assert.equal(formatTokenCount(999_600), '1.0M')
  assert.equal(formatTokenCount(9_960_000), '10M')
})

test('usage coverage labels never present partial numbers as complete', async () => {
  const { incompleteUsageNote, usageQualityLabel } = await import('../chatUsageFormat.js')
  assert.equal(usageQualityLabel('final'), null)
  assert.equal(usageQualityLabel('missing'), null)
  assert.match(usageQualityLabel('partial'), /at least/)
  assert.match(usageQualityLabel('unknown'), /Not known/)
  assert.equal(incompleteUsageNote(undefined), null)
  assert.equal(incompleteUsageNote({ runs_by_quality: { final: 3, missing: 2 } }), null)
  assert.equal(
    incompleteUsageNote({ runs_by_quality: { final: 1, partial: 1, unknown: 1 } }),
    '2 turns have partial or unknown usage. Totals add only what was reported.',
  )
  assert.match(incompleteUsageNote({ runs_by_quality: { unknown: 1 } }), /^1 turn has/)
})
