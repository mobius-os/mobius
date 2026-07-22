import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  resetComposerTextarea,
  resizeComposerTextarea,
} from '../composerTextareaSizing.js'

function textareaStub({ value = '', scrollHeight = 31, tall = false } = {}) {
  const classes = new Set(tall ? ['chat__pill--tall'] : [])
  const pill = {
    classList: {
      toggle(name, enabled) {
        if (enabled) classes.add(name)
        else classes.delete(name)
      },
      remove(name) { classes.delete(name) },
      contains(name) { return classes.has(name) },
    },
  }
  return {
    textarea: {
      value,
      scrollHeight,
      style: { height: tall ? '280px' : '' },
      closest: selector => selector === '.chat__pill' ? pill : null,
    },
    pill,
  }
}

test('foreground reconciliation collapses an empty textarea with stale tall geometry', () => {
  // Chromium can expose the old/capped client height as scrollHeight while a
  // focused empty textarea is returning inside a multi-pane layout. Empty must
  // not trust that measurement at all.
  const { textarea, pill } = textareaStub({ value: '', scrollHeight: 280, tall: true })

  assert.equal(resizeComposerTextarea(textarea), 0)
  assert.equal(textarea.style.height, 'auto')
  assert.equal(pill.classList.contains('chat__pill--tall'), false)
})

test('textarea sizing caps multi-line content and retains the tall alignment', () => {
  const { textarea, pill } = textareaStub({ value: 'many lines', scrollHeight: 420 })

  assert.equal(resizeComposerTextarea(textarea), 280)
  assert.equal(textarea.style.height, '280px')
  assert.equal(pill.classList.contains('chat__pill--tall'), true)
})

test('authoritative text can size before React commits it into the DOM value', () => {
  const { textarea, pill } = textareaStub({ value: '', scrollHeight: 120 })

  assert.equal(resizeComposerTextarea(textarea, 'voice transcript'), 120)
  assert.equal(textarea.style.height, '120px')
  assert.equal(pill.classList.contains('chat__pill--tall'), true)
})

test('hidden retained panes keep intrinsic height instead of receiving zero pixels', () => {
  const { textarea, pill } = textareaStub({ value: '', scrollHeight: 0, tall: true })

  assert.equal(resizeComposerTextarea(textarea), 0)
  assert.equal(textarea.style.height, 'auto')
  assert.equal(pill.classList.contains('chat__pill--tall'), false)
})

test('reset collapses immediately before React commits the empty value', () => {
  const { textarea, pill } = textareaStub({ value: 'old multi-line value', scrollHeight: 280, tall: true })

  resetComposerTextarea(textarea)
  assert.equal(textarea.style.height, 'auto')
  assert.equal(pill.classList.contains('chat__pill--tall'), false)
})

test('ChatView reconciles textarea geometry on value commits and foreground return', () => {
  const source = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
  const inputBarSource = readFileSync(new URL('../ChatInputBar.jsx', import.meta.url), 'utf8')
  const voiceSource = readFileSync(new URL('../useVoiceInput.js', import.meta.url), 'utf8')
  assert.match(source, /useLayoutEffect\(\(\) => \{[\s\S]*resizeComposerTextarea\(el, input\)[\s\S]*\}, \[chatId, hidden, input\]\)/)
  assert.match(source, /const reconcileForegroundGeometry = \(\) => \{[\s\S]*resizeComposerTextarea\(inputRef\.current, inputValueRef\.current\)[\s\S]*applySoon\(\)/)
  assert.match(source, /window\.addEventListener\('pageshow', reconcileForegroundGeometry\)/)
  assert.doesNotMatch(inputBarSource, /resizeComposerTextarea/)
  assert.doesNotMatch(voiceSource, /resizeComposerTextarea/)
  const resets = source.match(/resetComposerTextarea\(inputRef\.current\)/g) || []
  assert.equal(resets.length, 2, 'both queued and immediate sends collapse stale textarea geometry')
})
