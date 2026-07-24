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
  assert.match(hook, /bodyScrollLockCount/)
  assert.match(hook, /closeOnEscapeRef\.current/)
  assert.match(hook, /dialogStack\.at\(-1\) !== stackEntry/)
})

test('full-screen dialogs share one focus, inerting, and Escape contract', () => {
  const dialogs = [
    read('../../ui/ModelSheet.jsx'),
    read('../ManageModelsModal.jsx'),
    read('../../SettingsView/UpdateReviewModal.jsx'),
    read('../markdown/ImageLightbox.jsx'),
    read('../AgentContextInspector.jsx'),
    read('../ChatSummaryViewer.jsx'),
  ]

  for (const source of dialogs) {
    assert.match(source, /useDialogFocus\(\{/)
    assert.match(source, /role="dialog"/)
    assert.match(source, /aria-modal="true"/)
  }

  const manageModels = dialogs[1]
  const updateReview = dialogs[2]
  assert.match(manageModels, /ref=\{keepEditingRef\}/)
  assert.match(updateReview, /closeOnEscape: !applying/)
})

test('first-use guidance is a labeled non-modal region with a dismiss action', () => {
  const source = read('../../Walkthrough/WalkthroughOverlay.jsx')
  assert.match(source, /role="region"/)
  assert.match(source, /aria-labelledby="wt-title"/)
  assert.match(source, /aria-label="Dismiss welcome"/)
  assert.doesNotMatch(source, /aria-modal="true"/)
})

test('chat image preview actions use labeled buttons', () => {
  const attachments = read('../Attachments.jsx')
  const markdown = read('../markdown/InlineContent.jsx')
  assert.match(attachments, /<button[\s\S]*aria-label=\{`Open \$\{alt \|\| 'attached image'\} preview`\}/)
  assert.match(markdown, /<button[\s\S]*className="md-image-frame"[\s\S]*aria-label=\{`Open \$\{alt \|\| 'image'\} preview`\}/)
})

test('a restored image with no media token stops spinning and exposes its failure', () => {
  const composer = read('../ChatInputBar.jsx')
  assert.match(composer, /setTokenState\(\{ chatId, param, failed: !param \}\)/)
  assert.match(composer, /className="chat__attach-card-preview-error" role="status"/)
  assert.match(composer, /Preview unavailable/)
  assert.match(composer, /aria-label=\{`Remove \$\{chip\.name\}`\}/,
    'the failed preview must retain an explicit removal affordance')
})

test('QuestionCard gives the conditional Other field a durable accessible name', () => {
  const source = read('../QuestionCard.jsx')
  assert.match(source, /aria-label=\{`Other answer for: \$\{q\.question\}`\}/)
  assert.match(source, /placeholder="Type your answer…"/)
})

test('message sources expose list semantics, keyboard focus, and touch targets', () => {
  const source = read('../MessageSources.jsx')
  const msgContent = read('../MsgContent.jsx')
  const css = read('../ChatView.css')

  assert.match(source, /<section className="chat__sources" aria-label="Source links">/)
  assert.doesNotMatch(source, />Sources</,
    'source links should stand on their own at the end of the answer')
  assert.match(source, /<ul className="chat__sources-list">/)
  assert.match(msgContent,
    /msg\.role === 'assistant' && !isStreaming && \(\s*<MessageSources/,
    'source links should appear once the answer has settled, not move during streaming')
  assert.match(source, /<li key=\{source\.url\} className="chat__source-item">/)
  assert.match(source, /aria-label=\{`\$\{label\}.*opens in a new tab/)
  assert.match(source, /className="chat__source-icon" aria-hidden="true"/)
  assert.match(source, /sourceMark\(host\)/,
    'source recognition stays local instead of loading third-party favicons')
  assert.doesNotMatch(source, /<img|src=\{?[^\n]*favicon/i,
    'viewing an answer must not contact every cited site')
  assert.match(css, /\.chat__source-chip:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--accent\)/s)
  assert.match(css, /\.chat__source-chip\s*\{[^}]*border-radius:\s*999px/s)
  assert.match(css, /@media\s*\(pointer:\s*coarse\)\s*\{\s*\.chat__source-chip\s*\{\s*min-height:\s*44px/s)
})
