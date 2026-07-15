import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const component = readFileSync(new URL('../QuestionCard.jsx', import.meta.url), 'utf8')
const css = readFileSync(new URL('../QuestionCard.css', import.meta.url), 'utf8')

test('unanswered question cards do not have a stale gray state', () => {
  assert.doesNotMatch(component, /const stale = disabled && !answered/,
    'QuestionCard should not model unanswered questions as stale')
  assert.doesNotMatch(component, /qcard--stale/,
    'unanswered cards should not receive a stale visual class')
  assert.doesNotMatch(component, /This question is no longer active/,
    'unanswered cards should not tell the user the question expired')
  assert.match(component, /\{\(answered \|\| !disabled\) && \(\s*<button[\s\S]*className="qcard__submit"/,
    'submit button should remain in place after an answer is submitted')
  assert.match(component, /\{answered \? 'Submitted' : 'Submit'\}/,
    'the retained submit button should explain its answered state')
  assert.match(component, /\{!answered && !disabled && \(\s*<div className="qcard__hint"/,
    'selection hints should render only when the card is answerable')
})

test('question card css has no stale styling hook', () => {
  assert.doesNotMatch(css, /\.qcard--stale\s*\{[\s\S]*?\}/,
    'stale question styling should not come back')
  assert.doesNotMatch(css, /\.qcard__status\s*\{[\s\S]*?\}/,
    'expiration status styling should not come back')
})
