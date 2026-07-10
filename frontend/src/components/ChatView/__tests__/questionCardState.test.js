import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const component = readFileSync(new URL('../QuestionCard.jsx', import.meta.url), 'utf8')
const css = readFileSync(new URL('../QuestionCard.css', import.meta.url), 'utf8')

test('stale unanswered question cards explain that they are no longer active', () => {
  assert.match(component, /const stale = disabled && !answered/,
    'QuestionCard should distinguish stale unanswered cards from answered cards')
  assert.match(component, /className=\{`qcard[^`]*qcard--stale/,
    'stale cards should receive a visible state class')
  assert.match(component, /This question is no longer active/,
    'stale cards should explain why the controls are disabled')
  assert.match(component, /\{!answered && !disabled && \(\s*<button[\s\S]*className="qcard__submit"/,
    'submit button should render only while the card can still be answered')
  assert.match(component, /\{!answered && !disabled && \(\s*<div className="qcard__hint"/,
    'selection hints should not invite interaction on stale cards')
})

test('stale question cards have dedicated styling', () => {
  assert.match(css, /\.qcard--stale\s*\{[\s\S]*?\}/,
    'stale question cards should have a visible style hook')
  assert.match(css, /\.qcard__status\s*\{[\s\S]*?\}/,
    'stale question cards should style their explanatory status')
})
