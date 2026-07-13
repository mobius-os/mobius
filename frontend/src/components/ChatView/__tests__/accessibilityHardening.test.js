import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = relative => readFileSync(new URL(relative, import.meta.url), 'utf8')

test('InstallSheet uses the shared modal focus contract', () => {
  const source = read('../../Drawer/InstallSheet.jsx')
  const hook = read('../../../hooks/useDialogFocus.js')
  assert.match(source, /useDialogFocus\(\{/)
  assert.match(source, /ref=\{cardRef\}/)
  assert.match(hook, /event\.key === 'Escape'/)
  assert.match(hook, /event\.key !== 'Tab'/)
  assert.match(hook, /element\.inert = true/)
  assert.match(hook, /previouslyFocused\?\.focus/)
})

test('chat image preview actions use labeled buttons', () => {
  const attachments = read('../Attachments.jsx')
  const markdown = read('../markdown/InlineContent.jsx')
  assert.match(attachments, /<button[\s\S]*aria-label=\{`Open \$\{alt \|\| 'attached image'\} preview`\}/)
  assert.match(markdown, /<button[\s\S]*className="md-image-frame"[\s\S]*aria-label=\{`Open \$\{alt \|\| 'image'\} preview`\}/)
})

test('QuestionCard gives the conditional Other field a durable accessible name', () => {
  const source = read('../QuestionCard.jsx')
  assert.match(source, /aria-label=\{`Other answer for: \$\{q\.question\}`\}/)
  assert.match(source, /placeholder="Type your answer…"/)
})
