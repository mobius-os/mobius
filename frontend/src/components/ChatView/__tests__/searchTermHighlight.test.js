import { test } from 'node:test'
import assert from 'node:assert/strict'

import { termsFromSnippet } from '../searchTermHighlight.js'

const O = ''
const C = ''

test('extracts each marked surface form from a snippet', () => {
  const snip = `monthly ${O}budgeting${C} and ${O}budget${C} planning`
  assert.deepEqual(termsFromSnippet(snip), ['budgeting', 'budget'])
})

test('dedupes repeats and trims whitespace', () => {
  const snip = `${O}Atlas${C} vs ${O}Atlas${C} again`
  assert.deepEqual(termsFromSnippet(snip), ['Atlas'])
})

test('returns empty for a title-only (unmarked) or missing snippet', () => {
  assert.deepEqual(termsFromSnippet('no marks here'), [])
  assert.deepEqual(termsFromSnippet(null), [])
  assert.deepEqual(termsFromSnippet(''), [])
})
