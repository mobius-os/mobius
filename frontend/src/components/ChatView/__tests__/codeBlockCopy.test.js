/**
 * Markdown code blocks carry a copy button in the lower-right corner.
 *
 * Source-wiring tests: the button copies the raw token text through the
 * shared copyPlainText helper (clipboard API + textarea fallback), sits in a
 * non-scrolling wrapper so it stays pinned while long lines scroll, and
 * acknowledges with a check icon.
 *
 * Run with:
 *   cd frontend && node --test \
 *     src/components/ChatView/__tests__/codeBlockCopy.test.js
 */
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const blocksJsx = readFileSync(
  new URL('../markdown/blocks.jsx', import.meta.url), 'utf8')
const markdownCss = readFileSync(
  new URL('../markdown.css', import.meta.url), 'utf8')

function stripComments(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, '')
}

test('code block copy button copies raw text via the shared helper', () => {
  assert.match(blocksJsx, /import \{ copyPlainText \} from '\.\.\/messageCopy\.js'/,
    'reuse the shared clipboard helper (API + textarea fallback), not a bespoke path')
  assert.match(blocksJsx, /copyPlainText\(code\)/,
    'the raw token text is copied — never the highlighted HTML')
  assert.match(blocksJsx, /className=\{`md-code-copy/,
    'CodeBlock renders the md-code-copy button')
  assert.match(blocksJsx, /aria-label=\{copied \? 'Copied' : 'Copy code'\}/,
    'button announces its state to assistive tech')
})

test('copy button is pinned outside the scrolling pre', () => {
  // An absolutely positioned child of the scrolling <pre> would ride along
  // with long lines; the wrapper must own the positioning context.
  assert.match(blocksJsx, /<div className="md-code-wrap">\s*<pre className="md-code-block">/,
    'the button anchors to a wrapper around the <pre>, not inside it')

  const css = stripComments(markdownCss)
  const wrapRule = css.match(/\.md-code-wrap\s*\{[^}]*\}/)?.[0] || ''
  const blockRule = css.match(/\.md-code-block\s*\{[^}]*\}/)?.[0] || ''
  const copyRule = css.match(/\.md-code-copy\s*\{[^}]*\}/)?.[0] || ''

  assert.match(wrapRule, /position:\s*relative/,
    'wrapper establishes the positioning context')
  assert.match(copyRule, /position:\s*absolute/, 'button floats over the block')
  assert.match(copyRule, /right:\s*\d/, 'anchored to the right edge')
  assert.match(copyRule, /bottom:\s*\d/, 'anchored to the bottom edge (lower-right)')
  assert.match(blockRule, /padding:\s*14px\s+16px\s+40px/,
    'the code block reserves a row so the button cannot obscure its last line')
})

test('copied acknowledgement resets and cleans up its timer', () => {
  assert.match(blocksJsx, /setTimeout\(\(\) => setCopied\(false\)/,
    'check icon reverts to the copy glyph after a beat')
  assert.match(blocksJsx, /useEffect\(\(\) => \(\) => clearTimeout\(copyTimerRef\.current\), \[\]\)/,
    'pending timer is cleared on unmount')
})
