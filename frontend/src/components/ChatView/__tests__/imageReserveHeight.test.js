/**
 * Scroll-jump regression: chat images must reserve their height BEFORE the
 * bytes decode, so a late <img> load doesn't reflow the conversation below
 * them and yank the scroll position (CLS).
 *
 * The chat scroll contract disables Chrome's scroll anchoring
 * (`overflow-anchor: none` on `.chat__scroll`, see ARCHITECTURE.md's
 * "Chat scroll + steer contract") so the browser does NOT compensate for a
 * height change above the viewport. That makes any image that changes height
 * after first layout a visible jump. Two mechanisms keep image height stable:
 *
 *   1. `.md-image-frame` is an aspect-ratio box — it occupies its final height
 *      on the first layout pass, before the image decodes. The default ratio
 *      is landscape-leaning (agent screenshots are landscape) so the pre-load
 *      estimate is close to reality and the on-load delta is small. A
 *      `max-height` cap bounds extreme tall ratios. Once dimensions are known,
 *      portrait images also shrink the frame width so the cap does not create
 *      a wide gray letterbox around phone screenshots.
 *
 *   2. `.chat__msg` deliberately does NOT use `content-visibility: auto`. It
 *      was added as a phone-scroll perf hint (2459dff) but collapses off-screen
 *      message height, so the programmatic PIN_USER_MSG scroll write is clamped
 *      and 2nd+ sends land mid-screen instead of pinned to top. The pin is a
 *      non-negotiable invariant; the perf hint stays out (guard below).
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
const inlineContentJsx = readFileSync(resolve(here, '../markdown/InlineContent.jsx'), 'utf8')

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

  test('frame width can shrink after image dimensions load', () => {
    assert.match(body, /width:\s*min\(100%,\s*var\(--md-image-fit-width/,
      'portrait screenshots must shrink the frame width instead of showing side letterboxes')
  })

  test('frame background is transparent so any remaining containment blends into chat', () => {
    assert.match(body, /background:\s*transparent/,
      'image frames must not paint a gray box around portrait screenshots')
  })

  test('inner image is contained, so the cap never crops screenshots', () => {
    const imgBody = ruleBody(markdownCss, '.md-image ')
    assert.match(imgBody, /object-fit:\s*contain/,
      '.md-image must use object-fit: contain to fit inside the reserved/capped frame')
  })
})

describe('chat messages must NOT use content-visibility (it clamps the pin-scroll)', () => {
  // content-visibility: auto collapses an off-screen message's rendered height
  // to the contain-intrinsic-size estimate, so the synchronous PIN_USER_MSG
  // scroll write (useScrollMode) is clamped and the 2nd+ user message lands
  // mid-screen instead of pinned to the top. Added as a phone-scroll perf hint
  // (2459dff), it regressed the pin both times it was present. The pin is a
  // non-negotiable architecture contract, so the hint stays out.
  test('ChatView.css declares no content-visibility on chat messages', () => {
    // strip CSS comments so the prose explaining the removal does not count
    const cssNoComments = chatViewCss.replace(/\/\*[\s\S]*?\*\//g, '')
    assert.doesNotMatch(cssNoComments, /content-visibility/,
      'content-visibility on .chat__msg clamps the pin-scroll so 2nd+ sends are ' +
      'no longer pinned to top — do not re-add it (see useScrollMode PIN_USER_MSG)')
  })
})

describe('ExpandableImage reserves the frame BEFORE the token resolves (lever 3)', () => {
  // Strip comments so prose describing the old behavior does not match.
  const src = inlineContentJsx.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '')

  test('does not early-return null on a missing resolvedSrc (that inserted the frame late)', () => {
    assert.doesNotMatch(src, /if\s*\(\s*!resolvedSrc\s*\)\s*return\s+null/,
      'returning null until resolvedSrc let the whole .md-image-frame insert late and ' +
      'shove the surrounding text — the box must be reserved during the async token hop')
  })

  test('guards only the render on an INVALID href (rawSrc), reserving the frame otherwise', () => {
    assert.match(src, /if\s*\(\s*!rawSrc\s*\)\s*return\s+null/,
      'a blocked/empty href renders nothing, but a valid-but-unresolved href still reserves its frame')
  })

  test('the <img> element is gated on resolvedSrc while the frame is not', () => {
    // The frame span is unconditional; the img swaps in once resolvedSrc lands.
    assert.match(src, /resolvedSrc\s*&&\s*\(?\s*<img/,
      'the <img> must render only once resolvedSrc is ready, inside an always-present frame')
  })

  test('seeds first-paint aspect ratio from known dims (parseImageDims/imageVarsFromDims)', () => {
    assert.match(src, /parseImageDims/,
      'known dimensions carried in the markup must seed --md-image-ratio on the first paint')
    assert.match(src, /imageVarsFromDims/)
  })
})
