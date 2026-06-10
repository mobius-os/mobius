/**
 * Unit tests for the quick-action chip render condition and protocol helpers.
 *
 * ChatView renders chips when: embedded=true AND quickActions is a non-empty array.
 * When quickActions is absent/empty/non-array, a neutral fallback renders instead.
 * Max 4 chips are rendered regardless of input length.
 *
 * The actual React rendering is not exercised here — we test the logical
 * condition and the data-shaping helpers in isolation.
 *
 * Run with:
 *   cd frontend && npm run test:lib
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

// ── Chip render condition ─────────────────────────────────────────────────────
// Mirrors the condition in ChatView.jsx's embedded empty-state branch:
//   Array.isArray(quickActions) && quickActions.length > 0
function shouldRenderChips(quickActions) {
  return Array.isArray(quickActions) && quickActions.length > 0
}

test('renders chips when quickActions is a non-empty array', () => {
  assert.equal(shouldRenderChips([{ label: 'Fix errors', prompt: 'Fix the build errors' }]), true)
})

test('does not render chips when quickActions is null', () => {
  assert.equal(shouldRenderChips(null), false)
})

test('does not render chips when quickActions is undefined', () => {
  assert.equal(shouldRenderChips(undefined), false)
})

test('does not render chips when quickActions is an empty array', () => {
  assert.equal(shouldRenderChips([]), false)
})

test('does not render chips when quickActions is a non-array', () => {
  assert.equal(shouldRenderChips('chip'), false)
  assert.equal(shouldRenderChips(42), false)
  assert.equal(shouldRenderChips({ label: 'x', prompt: 'y' }), false)
})

// ── Max 4 limit ───────────────────────────────────────────────────────────────

test('slice(0, 4) limits chips to 4 items', () => {
  const many = [1, 2, 3, 4, 5, 6].map(i => ({ label: `L${i}`, prompt: `P${i}` }))
  const rendered = many.slice(0, 4)
  assert.equal(rendered.length, 4)
  assert.equal(rendered[0].label, 'L1')
  assert.equal(rendered[3].label, 'L4')
})

test('slice(0, 4) with fewer than 4 items renders all', () => {
  const two = [{ label: 'A', prompt: 'a' }, { label: 'B', prompt: 'b' }]
  assert.equal(two.slice(0, 4).length, 2)
})

// ── Runtime quickActions sanitizer ───────────────────────────────────────────
// Mirrors the mobius-runtime.js filter logic:
//   opts.quickActions
//     .filter(a => a && typeof a.label === 'string' && typeof a.prompt === 'string')
//     .slice(0, 4)
function sanitizeQuickActions(raw) {
  if (!Array.isArray(raw)) return undefined
  const filtered = raw
    .filter(a => a && typeof a.label === 'string' && typeof a.prompt === 'string')
    .slice(0, 4)
  return filtered.length > 0 ? filtered : undefined
}

test('sanitizer passes valid actions through', () => {
  const actions = [{ label: 'Fix', prompt: 'Fix errors' }]
  assert.deepEqual(sanitizeQuickActions(actions), actions)
})

test('sanitizer drops entries with missing label or prompt', () => {
  const mixed = [
    { label: 'Good', prompt: 'good prompt' },
    { label: 'No prompt' },           // missing prompt
    { prompt: 'no label' },            // missing label
    null,                              // null entry
    { label: 42, prompt: 'str' },      // non-string label
  ]
  const result = sanitizeQuickActions(mixed)
  assert.deepEqual(result, [{ label: 'Good', prompt: 'good prompt' }])
})

test('sanitizer caps at 4 items', () => {
  const six = Array.from({ length: 6 }, (_, i) => ({ label: `L${i}`, prompt: `P${i}` }))
  assert.equal(sanitizeQuickActions(six).length, 4)
})

test('sanitizer returns undefined for empty-after-filter arrays', () => {
  const bad = [null, undefined, { label: 1, prompt: 2 }]
  assert.equal(sanitizeQuickActions(bad), undefined)
})

test('sanitizer returns undefined for non-array input', () => {
  assert.equal(sanitizeQuickActions(null), undefined)
  assert.equal(sanitizeQuickActions('string'), undefined)
})

// ── INIT payload quickActions inclusion ──────────────────────────────────────
// Mirrors the runtime sendInit logic: only include quickActions in the
// INIT message when the sanitized array is non-empty.
function buildInitPayload(instanceId, chatId, pickerOn, quickActions) {
  const msg = { type: 'moebius:chat-embed:init', instanceId, chatId, picker: pickerOn }
  if (quickActions && quickActions.length > 0) msg.quickActions = quickActions
  return msg
}

test('INIT payload includes quickActions when non-empty', () => {
  const actions = [{ label: 'A', prompt: 'a' }]
  const msg = buildInitPayload('inst', 'cid', true, actions)
  assert.deepEqual(msg.quickActions, actions)
})

test('INIT payload omits quickActions when absent or empty', () => {
  assert.equal(buildInitPayload('inst', 'cid', true, null).quickActions, undefined)
  assert.equal(buildInitPayload('inst', 'cid', true, []).quickActions, undefined)
  assert.equal(buildInitPayload('inst', 'cid', true, undefined).quickActions, undefined)
})
