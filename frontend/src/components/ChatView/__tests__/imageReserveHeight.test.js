/**
 * Scroll-jump regression: chat images must reserve their height BEFORE the
 * bytes decode, so a late <img> load doesn't reflow the conversation below
 * them and yank the scroll position (CLS).
 *
 * The chat scroll contract disables Chrome's scroll anchoring
 * (`overflow-anchor: none` on `.chat__scroll`, see CLAUDE.md "Chat UX —
 * non-negotiable constraints") so the browser does NOT compensate for a
 * height change above the viewport. That makes any image that changes height
 * after first layout a visible jump. Two mechanisms keep image height stable:
 *
 *   1. `.md-image-frame` is an aspect-ratio box — it occupies its final height
 *      on the first layout pass, before the image decodes. The default ratio
 *      is landscape-leaning (agent screenshots are landscape) so the pre-load
 *      estimate is close to reality and the on-load delta is small. A
 *      `max-height` cap bounds extreme tall ratios; `object-fit: contain`
 *      letterboxes inside the reserved box.
 *
 *   2. `.chat__msg` uses `content-visibility: auto` with a 120px intrinsic-size
 *      estimate. That estimate is wildly wrong for a message holding a
 *      screenshot, so image-bearing messages are exempted — they render
 *      eagerly and keep their true height at all times, avoiding the
 *      scroll-into-view re-measure jump.
 *
 * These are CSS-shape assertions (no DOM/layout engine). They pin the
 * load-bearing declarations so a future edit that re-introduces the jump
 * (portrait default, dropped reservation, removed exemption) fails here.
 *
 * Run with:
 *   cd frontend && node --loader=./src/lib/__tests__/vite-env-loader.mjs \
 *     --test src/components/ChatView/__tests__/imageReserveHeight.test.js
 */
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const markdownCss = readFileSync(resolve(here, '../markdown.css'), 'utf8')
const chatViewCss = readFileSync(resolve(here, '../ChatView.css'), 'utf8')

/** Extract the body `{ ... }` of the first rule whose selector list contains
 *  `selector`. Brace-counting so nested at-rules don't trip a naive regex. */
function ruleBody(css, selector) {
  const idx = css.indexOf(selector)
  assert.ok(idx !== -1, `selector not found: ${selector}`)
  const open = css.indexOf('{', idx)
  assert.ok(open !== -1, `no rule body for: ${selector}`)
  let depth = 0
  for (let i = open; i < css.length; i++) {
    if (css[i] === '{') depth++
    else if (css[i] === '}') {
      depth--
      if (depth === 0) return css.slice(open + 1, i)
    }
  }
  throw new Error(`unterminated rule body for: ${selector}`)
}

describe('.md-image-frame reserves height before image decode', () => {
  const body = ruleBody(markdownCss, '.md-image-frame')

  test('uses an aspect-ratio box (height derived from width on first layout)', () => {
    assert.match(body, /aspect-ratio:\s*var\(--md-image-ratio/,
      'frame must reserve height via aspect-ratio, driven by --md-image-ratio')
  })

  test('default ratio is landscape-leaning (so a wide screenshot does not collapse the box)', () => {
    // Capture the fallback inside var(--md-image-ratio, <fallback>).
    const m = body.match(/var\(--md-image-ratio,\s*([0-9]+)\s*\/\s*([0-9]+)\s*\)/)
    assert.ok(m, 'must declare a default ratio fallback')
    const w = Number(m[1]), h = Number(m[2])
    assert.ok(w >= h,
      `default ratio ${w}/${h} must be landscape-or-square (w >= h); a portrait ` +
      `default reserves a tall box that collapses when a wide image decodes — the bug`)
  })

  test('caps reserved height so one image cannot claim the whole viewport', () => {
    assert.match(body, /max-height:/,
      'frame must cap reserved height for extreme tall ratios')
  })

  test('inner image is contained, so the cap letterboxes instead of cropping', () => {
    const imgBody = ruleBody(markdownCss, '.md-image ')
    assert.match(imgBody, /object-fit:\s*contain/,
      '.md-image must use object-fit: contain to fit inside the reserved/capped frame')
  })
})

describe('image-bearing messages are exempt from content-visibility skipping', () => {
  test('base .chat__msg still skips off-screen render work (perf intact)', () => {
    const body = ruleBody(chatViewCss, '.chat__msg {')
    assert.match(body, /content-visibility:\s*auto/,
      'text/tool messages must keep content-visibility: auto')
    assert.match(body, /contain-intrinsic-size:\s*auto\s+120px/,
      'base intrinsic-size estimate stays 120px for non-image messages')
  })

  test('messages containing a markdown image opt out of the 120px estimate', () => {
    assert.match(chatViewCss, /\.chat__msg:has\(\.md-image-frame\)/,
      'image-bearing messages must be selected via :has(.md-image-frame)')
    const body = ruleBody(chatViewCss, '.chat__msg:has(.md-image-frame)')
    assert.match(body, /content-visibility:\s*visible/,
      'image-bearing messages must render eagerly (no off-screen 120px guess)')
  })

  test('messages containing an attachment thumbnail strip also opt out', () => {
    assert.match(chatViewCss, /\.chat__msg:has\(\.chat__attach-images\)/,
      'attachment-bearing messages must also opt out via :has(.chat__attach-images)')
  })
})
