import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'

// CSS invariants the manual scroll model depends on (CLAUDE.md "Chat UX —
// non-negotiable constraints"). These live in ChatView.css but were asserted
// nowhere, so a cascade edit could silently remove them and re-introduce the
// exact bugs the comments warn about. Source-scan lock-ins, scoped to the
// `.chat__scroll` / `.chat__list` / `.spacer-dynamic` rules.

const dir = dirname(fileURLToPath(import.meta.url))
const css = readFileSync(join(dir, '..', 'ChatView.css'), 'utf8')

function ruleBody(selector) {
  const start = css.indexOf(`\n${selector} {`)
  assert.notEqual(start, -1, `selector ${selector} not found in ChatView.css`)
  const open = css.indexOf('{', start)
  const close = css.indexOf('}', open)
  // Strip block comments so a comment MENTIONING a property (e.g. the
  // "/* no transition */" note on the empty .spacer-dynamic rule) is not read
  // as the property itself.
  return css.slice(open + 1, close).replace(/\/\*[\s\S]*?\*\//g, '')
}

test('.chat__scroll disables browser scroll anchoring', () => {
  // Chrome's overflow-anchor fights the manual spacer + JS scrollTop model;
  // it MUST stay off or the pin/anchor machinery double-corrects.
  assert.match(ruleBody('.chat__scroll'), /overflow-anchor:\s*none/)
})

test('.chat__scroll contains its overscroll and is a positioning context', () => {
  const body = ruleBody('.chat__scroll')
  assert.match(body, /overscroll-behavior-y:\s*contain/)
  assert.match(body, /position:\s*relative/)
})

test('nested chat readers allow browser-native vertical handoff to their transcript', () => {
  for (const selector of [
    '.chat__tool-detail', '.chat__marker-body', '.chat__document-card-reader',
  ]) {
    assert.match(ruleBody(selector), /overscroll-behavior-y:\s*auto/)
  }
  const questionCss = readFileSync(join(dir, '..', 'QuestionCard.css'), 'utf8')
  assert.match(questionCss, /\.qcard__input\s*\{[^}]*overscroll-behavior-y:\s*auto/)
})

test('project copy files hand off inside their dialog, not to the background', () => {
  const copyCss = readFileSync(join(dir, '..', '..', 'Projects', 'ProjectCopy.css'), 'utf8')
  assert.match(copyCss, /\.project-copy__files\s*\{[^}]*overscroll-behavior:\s*contain auto/)
  assert.match(copyCss, /\.project-copy-dialog\s*\{[^}]*overscroll-behavior:\s*contain;/)
})

test('the composer and transcript use the same shell-owned bottom inset', () => {
  const foot = ruleBody('.chat__foot')
  const list = ruleBody('.chat__list')

  const inset = /var\(--shell-safe-bottom-inset,\s*env\(safe-area-inset-bottom,\s*0px\)\)/
  const extra = /var\(--chat-foot-extra-bottom,\s*0px\)/
  assert.match(foot, new RegExp(`bottom:\\s*calc\\(${inset.source}\\s*\\+\\s*${extra.source}\\)`))
  assert.match(
    list,
    new RegExp(`var\\(--composer-h,\\s*80px\\)\\s*\\+\\s*${inset.source}\\s*\\+\\s*${extra.source}\\s*\\+\\s*16px`),
  )
  assert.doesNotMatch(foot, /safe-area-inset-bottom[\s\S]*-\s*14px/)
  assert.doesNotMatch(list, /safe-area-inset-bottom[\s\S]*-\s*14px/)
})

test('the composer backdrop fills the safe area without moving controls into it', () => {
  const backdrop = ruleBody('.chat__foot::before')
  const embeddedBackdrop = ruleBody('.chat-embed .chat__foot::before')

  assert.match(
    backdrop,
    /bottom:\s*calc\(0px\s*-\s*var\(--shell-safe-bottom-inset,\s*env\(safe-area-inset-bottom,\s*0px\)\)\)/,
  )
  assert.match(embeddedBackdrop, /bottom:\s*0/)
})

test('the durable-wait disclosure and cancel controls receive footer taps', () => {
  const foot = ruleBody('.chat__foot')
  assert.match(foot, /pointer-events:\s*none/)
  assert.match(
    css,
    /\.chat__foot \.chat__wait-summary,[\s\S]*?\.chat__foot \.chat__wait-cancel,[\s\S]*?\{\s*pointer-events:\s*auto;\s*\}/,
    'the pointer-transparent footer must opt both wait controls back in',
  )
})

test('.spacer-dynamic has no CSS transition (instant height change)', () => {
  // A transition on the spacer height makes every pin/anchor correction animate
  // and desyncs the scrollTop math from the painted layout.
  const body = ruleBody('.spacer-dynamic')
  assert.doesNotMatch(body, /transition/)
})
