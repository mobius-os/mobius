import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  textareaUsesNativeSizing,
  reconcileComposerTextarea,
  resetComposerTextarea,
  resizeComposerTextarea,
  syncComposerTallState,
  textareaHasWrappedLines,
} from '../composerTextareaSizing.js'

function textareaStub({ value = '', scrollHeight = 31, tall = false } = {}) {
  const attributes = new Set(tall ? ['data-composer-tall'] : [])
  const pill = {
    className: 'chat__pill',
    toggleAttribute(name, enabled) {
      if (enabled) attributes.add(name)
      else attributes.delete(name)
    },
    removeAttribute(name) { attributes.delete(name) },
    hasAttribute(name) { return attributes.has(name) },
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
  assert.equal(pill.hasAttribute('data-composer-tall'), false)
})

test('textarea sizing caps multi-line content and retains the tall alignment', () => {
  const { textarea, pill } = textareaStub({ value: 'many lines', scrollHeight: 420 })

  assert.equal(resizeComposerTextarea(textarea), 280)
  assert.equal(textarea.style.height, '280px')
  assert.equal(pill.hasAttribute('data-composer-tall'), true)
})

test('authoritative text can size before React commits it into the DOM value', () => {
  const { textarea, pill } = textareaStub({ value: '', scrollHeight: 120 })

  assert.equal(resizeComposerTextarea(textarea, 'voice transcript'), 120)
  assert.equal(textarea.style.height, '120px')
  assert.equal(pill.hasAttribute('data-composer-tall'), true)
})

test('hidden retained panes keep intrinsic height instead of receiving zero pixels', () => {
  const { textarea, pill } = textareaStub({ value: '', scrollHeight: 0, tall: true })

  assert.equal(resizeComposerTextarea(textarea), 0)
  assert.equal(textarea.style.height, 'auto')
  assert.equal(pill.hasAttribute('data-composer-tall'), false)
})

test('reset collapses immediately before React commits the empty value', () => {
  const { textarea, pill } = textareaStub({ value: 'old multi-line value', scrollHeight: 280, tall: true })

  resetComposerTextarea(textarea)
  assert.equal(textarea.style.height, 'auto')
  assert.equal(pill.hasAttribute('data-composer-tall'), false)
})

test('authoritative empty state clears stale native inline geometry', () => {
  const { textarea, pill } = textareaStub({
    value: 'browser-restored stale value',
    scrollHeight: 280,
    tall: true,
  })

  assert.equal(reconcileComposerTextarea(textarea, ''), 0)
  assert.equal(textarea.style.height, 'auto')
  assert.equal(pill.hasAttribute('data-composer-tall'), false)
})

test('native content sizing is capability-gated without browser sniffing', () => {
  assert.equal(textareaUsesNativeSizing({
    supports: (property, value) => (
      property === 'field-sizing' && value === 'content'
    ),
  }), true)
  assert.equal(textareaUsesNativeSizing({ supports: () => false }), false)
  assert.equal(textareaUsesNativeSizing(null), false)
})

test('native resize observation owns only the tall alignment attribute', () => {
  const { textarea, pill } = textareaStub()
  assert.equal(syncComposerTallState(textarea, 31), 31)
  assert.equal(pill.hasAttribute('data-composer-tall'), false)
  assert.equal(syncComposerTallState(textarea, 55), 55)
  assert.equal(pill.hasAttribute('data-composer-tall'), true)
})

test('attachment class changes preserve measured multiline alignment', () => {
  const { textarea, pill } = textareaStub()
  syncComposerTallState(textarea, 78)

  pill.className = 'chat__pill chat__pill--with-attach'
  assert.equal(pill.hasAttribute('data-composer-tall'), true)
  pill.className = 'chat__pill'
  assert.equal(pill.hasAttribute('data-composer-tall'), true)

  resetComposerTextarea(textarea)
  assert.equal(pill.hasAttribute('data-composer-tall'), false)
})

test('ChatView reconciles textarea geometry on value commits and foreground return', () => {
  const source = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
  const inputBarSource = readFileSync(new URL('../MarkdownComposer.jsx', import.meta.url), 'utf8')
  const voiceSource = readFileSync(new URL('../useVoiceInput.js', import.meta.url), 'utf8')
  assert.match(source, /useLayoutEffect\(\(\) => \{[\s\S]*reconcileComposerTextarea\(el, input\)[\s\S]*\}, \[chatId, hidden, input\]\)/)
  assert.match(source, /const reconcileForegroundGeometry = \(\) => \{[\s\S]*reconcileComposerTextarea\(inputRef\.current, inputValueRef\.current\)[\s\S]*publishComposerRoom\(\)/)
  assert.match(source, /window\.addEventListener\('pageshow', reconcileForegroundGeometry\)/)
  assert.match(inputBarSource, /new ResizeObserver\(/)
  assert.match(inputBarSource, /syncComposerTallState\(/)
  assert.doesNotMatch(voiceSource, /resizeComposerTextarea/)
  const resets = source.match(/resetComposerTextarea\(inputRef\.current\)/g) || []
  assert.equal(resets.length, 1, 'one composer-clearing helper collapses stale textarea geometry')
  assert.match(source, /function clearComposerForSend\(\) \{[\s\S]*?if \(inputRef\.current\) resetComposerTextarea\(inputRef\.current\)\n\s+\}/)
  assert.equal((source.match(/\n\s+clearComposerForSend\(\)\n/g) || []).length, 2, 'both queued and immediate sends use it')
})

test('wrapped-line detection compares rendered rows with source lines', () => {
  // The composer's own metrics: 16px text at line-height 1.45, 4px padding.
  const style = () => ({ lineHeight: '23.2px', paddingTop: '4px', paddingBottom: '4px' })
  const rows = (value, visualRows) => ({ value, scrollHeight: Math.round(visualRows * 23.2 + 8) })

  assert.equal(textareaHasWrappedLines(rows('one line', 1), style), false)
  assert.equal(textareaHasWrappedLines(rows('two\nlines', 2), style), false)
  assert.equal(textareaHasWrappedLines(rows('a long line that wraps', 2), style), true)
  assert.equal(textareaHasWrappedLines(rows('first\nsecond wraps', 3), style), true)
})

test('wrapped-line detection answers true when geometry is unknown', () => {
  const textarea = { value: 'text', scrollHeight: 31 }
  assert.equal(textareaHasWrappedLines(null), true)
  assert.equal(textareaHasWrappedLines(textarea, null), true)
  assert.equal(textareaHasWrappedLines(textarea, () => ({ lineHeight: 'normal' })), true)
  assert.equal(textareaHasWrappedLines({ value: 'text', scrollHeight: 0 }, () => ({ lineHeight: '23.2px' })), true)
})
