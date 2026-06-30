import assert from 'node:assert/strict'
import { mkdir, rm } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import test from 'node:test'

const execFileAsync = promisify(execFile)
const root = dirname(fileURLToPath(import.meta.url))
const buildDir = join(root, '.build')
const bundled = join(buildDir, 'index.mjs')

async function bundle() {
  await rm(buildDir, { recursive: true, force: true })
  await mkdir(buildDir, { recursive: true })
  await execFileAsync('/home/hmzmrzx/projects/mobius/frontend/node_modules/.bin/esbuild', [
    join(root, '..', 'index.jsx'),
    '--bundle',
    '--format=esm',
    '--platform=node',
    '--jsx=automatic',
    `--outfile=${bundled}`,
  ], { env: { ...process.env, NODE_PATH: '/home/hmzmrzx/projects/mobius/frontend/node_modules' } })
  return import(pathToFileURL(bundled))
}

test('hardenReportHtml injects a restrictive CSP into full reports', async () => {
  const { hardenReportHtml } = await bundle()

  const html = '<!doctype html><html><head><title>Brief</title></head><body><h1>Morning</h1></body></html>'
  const hardened = hardenReportHtml(html)

  assert.match(hardened, /Content-Security-Policy/)
  assert.match(hardened, /default-src 'none'/)
  assert.match(hardened, /style-src 'unsafe-inline'/)
  assert.match(hardened, /img-src data: blob:/)
  assert.equal((hardened.match(/Content-Security-Policy/g) || []).length, 1)
  assert.ok(hardened.indexOf('Content-Security-Policy') < hardened.indexOf('<title>Brief</title>'))
})

test('hardenReportHtml wraps fragments in a complete document', async () => {
  const { hardenReportHtml } = await bundle()

  const hardened = hardenReportHtml('<main>hello</main>')

  assert.match(hardened, /^<!doctype html>/i)
  assert.match(hardened, /<body><main>hello<\/main><\/body>/)
})

test('hardenReportHtml injects height-reporter script that postMessages reflection:brief-height', async () => {
  const { hardenReportHtml } = await bundle()

  const html = '<!doctype html><html><head><title>Brief</title></head><body><p>hi</p></body></html>'
  const hardened = hardenReportHtml(html)

  // script-src 'unsafe-inline' must be present (required for injected script)
  assert.match(hardened, /script-src 'unsafe-inline'/)
  // The height reporter script must be present
  assert.match(hardened, /reflection:brief-height/)
  assert.match(hardened, /postMessage/)
  // The reporter must measure the documentElement border-box height —
  // viewport-independent, so a transient over-measurement can shrink back.
  assert.match(hardened, /document\.documentElement\.getBoundingClientRect\(\)\.height/)
  // scrollHeight is floored at the iframe's own viewport height, so a
  // transient over-measurement mid-reflow (classic scrollbars re-wrapping
  // text) would ratchet the iframe taller forever. The reporter must not
  // use it.
  assert.doesNotMatch(hardened, /scrollHeight/)
  // Script injected before existing head content
  assert.ok(
    hardened.indexOf('reflection:brief-height') < hardened.indexOf('<title>Brief</title>'),
    'height reporter should appear before existing head content',
  )
})

test('hardenReportHtml injects overflow guards so a brief never scrolls horizontally', async () => {
  const { hardenReportHtml } = await bundle()

  const html = '<!doctype html><html><head><title>Brief</title></head><body><h1>Morning</h1></body></html>'
  const hardened = hardenReportHtml(html)

  // html/body boxed to the viewport, no sideways scroll
  assert.match(hardened, /html,\s*body\s*\{[^}]*overflow-x:\s*hidden/)
  assert.match(hardened, /html,\s*body\s*\{[^}]*max-width:\s*100%/)
  // box-sizing reset + media/table capped to 100%
  assert.match(hardened, /box-sizing:\s*border-box/)
  assert.match(hardened, /img,\s*svg,\s*video,\s*canvas\s*\{[^}]*max-width:\s*100%/)
  // wide tables become their own scroller instead of pushing the page wide
  assert.match(hardened, /table\s*\{[^}]*display:\s*block[^}]*overflow-x:\s*auto/)
  // long code/pre wraps rather than overflowing
  assert.match(hardened, /white-space:\s*pre-wrap/)
  assert.match(hardened, /word-break:\s*break-word/)

  // Base style must come before the brief's own head content so the template's
  // richer rules win on the cascade.
  assert.ok(
    hardened.indexOf('overflow-x: hidden') < hardened.indexOf('<title>Brief</title>'),
    'base overflow style should appear before existing head content',
  )
})

test('hardenReportHtml styles details/summary drill-down and the questions card', async () => {
  const { hardenReportHtml } = await bundle()

  const hardened = hardenReportHtml('<main>hi</main>')

  // <details>/<summary> get native-feeling chrome (so the brief can stay
  // high-level by default and reveal detail on tap)
  assert.match(hardened, /details\s*\{/)
  assert.match(hardened, /details\s*>\s*summary\s*\{/)
  assert.match(hardened, /details\[open\]\s*>\s*summary::before/)
  // the end-of-brief "questions for you" card has a styled block
  assert.match(hardened, /\.brief-questions\s*\{/)
})

// ---------------------------------------------------------------------------
// In-report question carrier: the agent appends an inert JSON carrier as a
// sibling after the brief root. The React layer must EXTRACT the questions,
// STRIP the carrier before the iframe srcDoc, and silently ignore a malformed
// or absent carrier. (Native tap-card rendering + answer persistence are
// exercised in the app; here we lock the pure extract/strip contract that
// hardenReportHtml depends on running first.)
// ---------------------------------------------------------------------------

const CARRIER_BRIEF = [
  '<!doctype html><html><head><title>Brief</title></head><body>',
  '<main><h1>Good morning</h1><p>Here is your brief.</p></main>',
  '<section class="report-questions" data-report-questions>',
  '<h2>A few questions</h2><p class="rq-note">Tap to answer.</p>',
  '<script type="application/mobius-questions+json">',
  '{"version":1,"questions":[',
  '{"question":"Which apps should I prioritise?","header":"Focus","multiSelect":true,',
  '"options":[{"label":"Notes","description":"the notes app"},{"label":"Habits"}]},',
  '{"question":"How long should the brief be?","header":"Length","multiSelect":false,',
  '"options":[{"label":"Short"},{"label":"Detailed"}]}',
  ']}',
  '</script></section>',
  '</body></html>',
].join('\n')

test('extractReportQuestions parses the carrier into the exact QuestionCard shape', async () => {
  const { extractReportQuestions } = await bundle()
  const { questions } = extractReportQuestions(CARRIER_BRIEF)

  assert.equal(questions.length, 2)
  assert.equal(questions[0].question, 'Which apps should I prioritise?')
  assert.equal(questions[0].header, 'Focus')
  assert.equal(questions[0].multiSelect, true)
  assert.deepEqual(questions[0].options, [
    { label: 'Notes', description: 'the notes app' },
    { label: 'Habits' },
  ])
  assert.equal(questions[1].multiSelect, false)
  assert.deepEqual(questions[1].options, [{ label: 'Short' }, { label: 'Detailed' }])
})

test('extractReportQuestions strips the carrier BEFORE srcDoc (no data-report-questions left)', async () => {
  const { extractReportQuestions, hardenReportHtml } = await bundle()
  const { html } = extractReportQuestions(CARRIER_BRIEF)

  // The visible section shell AND the inert carrier script must be gone so
  // they never reach the sandboxed iframe.
  assert.doesNotMatch(html, /data-report-questions/i)
  assert.doesNotMatch(html, /report-questions/i)
  assert.doesNotMatch(html, /mobius-questions\+json/i)
  // The brief body itself survives the strip.
  assert.match(html, /Good morning/)
  assert.match(html, /<title>Brief<\/title>/)
  // And hardening the stripped HTML never re-introduces the carrier.
  assert.doesNotMatch(hardenReportHtml(html), /data-report-questions/i)
})

test('extractReportQuestions strips a bare carrier script with no wrapping section', async () => {
  const { extractReportQuestions } = await bundle()
  const bare = '<main><h1>Hi</h1></main>\n'
    + '<script type="application/mobius-questions+json">'
    + '{"questions":[{"question":"Q?","options":[{"label":"A"}]}]}</script>'
  const { html, questions } = extractReportQuestions(bare)

  assert.equal(questions.length, 1)
  assert.doesNotMatch(html, /mobius-questions\+json/i)
  assert.match(html, /Hi/)
})

test('a malformed carrier yields no cards and leaves the brief intact', async () => {
  const { extractReportQuestions } = await bundle()
  const bad = '<!doctype html><body><main><h1>Morning</h1></main>'
    + '<section data-report-questions>'
    + '<script type="application/mobius-questions+json">{ this is not json }</script>'
    + '</section></body>'
  const { html, questions } = extractReportQuestions(bad)

  assert.deepEqual(questions, [])
  // Even with bad JSON the section is removed so no empty heading renders,
  // and the brief content is untouched.
  assert.doesNotMatch(html, /data-report-questions/i)
  assert.match(html, /Morning/)
})

test('an absent carrier returns no questions and the HTML unchanged', async () => {
  const { extractReportQuestions } = await bundle()
  const plain = '<!doctype html><body><main><h1>Just a brief</h1></main></body>'
  const { html, questions } = extractReportQuestions(plain)

  assert.deepEqual(questions, [])
  assert.equal(html, plain)
})

test('sanitizeQuestions drops half-formed entries and caps at 3 questions / 6 options', async () => {
  const { sanitizeQuestions } = await bundle()
  const out = sanitizeQuestions([
    { question: '', options: [{ label: 'x' }] },     // empty question -> drop
    { question: 'no opts', options: [] },            // no options -> drop
    { question: 'ok1', options: [{ label: 'a' }] },
    { question: 'ok2', options: [{ label: 'b' }] },
    { question: 'ok3', options: [{ label: 'c' }] },
    { question: 'ok4', options: [{ label: 'd' }] },  // 4th valid -> capped out
  ])
  assert.equal(out.length, 3)
  assert.deepEqual(out.map((q) => q.question), ['ok1', 'ok2', 'ok3'])

  const capped = sanitizeQuestions([{
    question: 'many',
    options: Array.from({ length: 9 }, (_, i) => ({ label: 'o' + i })),
  }])
  assert.equal(capped[0].options.length, 6)
})

test('extractReportQuestions never throws on non-string input', async () => {
  const { extractReportQuestions, sanitizeQuestions } = await bundle()
  assert.deepEqual(extractReportQuestions(null), { html: '', questions: [] })
  assert.deepEqual(extractReportQuestions(undefined), { html: '', questions: [] })
  assert.deepEqual(sanitizeQuestions('nope'), [])
})

// ---------------------------------------------------------------------------
// Robustness: the carrier strip must survive adversarial JSON content and a
// whitespace-tolerant type attribute, must remove EVERY carrier, and the
// question set must carry unique texts (Codex review).
// ---------------------------------------------------------------------------

test('a literal </section> inside an option label does not leak the carrier', async () => {
  const { extractReportQuestions } = await bundle()
  // An option label that contains the literal string "</section>" — a naive
  // non-greedy <section>…</section> strip would stop here and leak the rest.
  const tricky = [
    '<!doctype html><html><head><title>Brief</title></head><body>',
    '<main><h1>Good morning</h1></main>',
    '<section class="report-questions" data-report-questions>',
    '<h2>Questions</h2>',
    '<script type="application/mobius-questions+json">',
    '{"questions":[{"question":"Pick a tag","options":[',
    '{"label":"close </section> early"},{"label":"normal"}]}]}',
    '</script></section>',
    '</body></html>',
  ].join('\n')
  const { html, questions } = extractReportQuestions(tricky)

  // The carrier is fully gone — no marker, no MIME, no script, no leaked label.
  assert.doesNotMatch(html, /data-report-questions/i)
  assert.doesNotMatch(html, /application\/mobius-questions\+json/i)
  assert.doesNotMatch(html, /<script/i)
  assert.doesNotMatch(html, /close <\/section> early/)
  // The brief body itself survives.
  assert.match(html, /Good morning/)
  // And the questions still parse correctly through the carrier.
  assert.equal(questions.length, 1)
  assert.equal(questions[0].question, 'Pick a tag')
  assert.deepEqual(questions[0].options, [
    { label: 'close </section> early' },
    { label: 'normal' },
  ])
})

test('a whitespace-padded type attribute (type = "…") is still extracted and stripped', async () => {
  const { extractReportQuestions } = await bundle()
  const padded = [
    '<main><h1>Hi</h1></main>',
    '<section data-report-questions>',
    '<script type = "application/mobius-questions+json">',
    '{"questions":[{"question":"Q?","options":[{"label":"A"}]}]}',
    '</script></section>',
  ].join('\n')
  const { html, questions } = extractReportQuestions(padded)

  assert.equal(questions.length, 1)
  assert.equal(questions[0].question, 'Q?')
  assert.doesNotMatch(html, /data-report-questions/i)
  assert.doesNotMatch(html, /application\/mobius-questions\+json/i)
  assert.doesNotMatch(html, /<script/i)
  assert.match(html, /Hi/)
})

test('two carriers in one html are BOTH fully stripped', async () => {
  const { extractReportQuestions } = await bundle()
  const two = [
    '<main><h1>Hi</h1></main>',
    '<section data-report-questions>',
    '<script type="application/mobius-questions+json">',
    '{"questions":[{"question":"First?","options":[{"label":"A"}]}]}',
    '</script></section>',
    '<section data-report-questions>',
    '<script type="application/mobius-questions+json">',
    '{"questions":[{"question":"Second?","options":[{"label":"B"}]}]}',
    '</script></section>',
  ].join('\n')
  const { html, questions } = extractReportQuestions(two)

  // First carrier's JSON is what gets extracted.
  assert.equal(questions.length, 1)
  assert.equal(questions[0].question, 'First?')
  // Neither carrier survives.
  assert.doesNotMatch(html, /data-report-questions/i)
  assert.doesNotMatch(html, /application\/mobius-questions\+json/i)
  assert.doesNotMatch(html, /<script/i)
  assert.match(html, /Hi/)
})

test('sanitizeQuestions dedupes questions with identical text', async () => {
  const { sanitizeQuestions } = await bundle()
  const out = sanitizeQuestions([
    { question: 'Same text', options: [{ label: 'A' }] },
    { question: 'Same text', options: [{ label: 'B' }] },
  ])
  assert.equal(out.length, 1)
  assert.equal(out[0].question, 'Same text')
  assert.deepEqual(out[0].options, [{ label: 'A' }])
})
