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

test('ShareAppSheet uses the shared modal focus contract', () => {
  const source = read('../../Drawer/ShareAppSheet.jsx')
  assert.match(source, /useDialogFocus\(\{/)
  assert.match(source, /ref=\{cardRef\}/)
  assert.match(source, /initialFocusRef: primaryFocusRef/)
  assert.match(source, /role="dialog"/)
  assert.match(source, /aria-modal="true"/)
  assert.match(source, /aria-labelledby="sas-title"/)
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
  assert.match(source, /aria-labelledby="wt-install-title"/)
  assert.match(source, /aria-expanded=/)
  assert.match(source, /role="status"/)
  assert.doesNotMatch(source, /aria-modal="true"/)
})

test('chat image preview actions use labeled buttons', () => {
  const attachments = read('../Attachments.jsx')
  const composer = read('../ChatInputBar.jsx')
  const preview = read('../ImagePreviewButton.jsx')
  const markdown = read('../markdown/InlineContent.jsx')
  assert.match(attachments, /<ImagePreviewButton/)
  assert.match(composer, /aria-label=\{`View \$\{chip\.name\} full screen`\}/)
  assert.match(preview, /aria-label=\{`Open \$\{alt \|\| 'image'\} preview`\}/)
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

test('QuestionCard gives the custom answer area a durable accessible name', () => {
  const source = read('../QuestionCard.jsx')
  assert.match(source, /aria-label=\{`Custom answer for: \$\{question\}`\}/)
  assert.match(source, /placeholder=\{answered \? 'No custom answer' : 'Or type your own answer…'\}/)
})

test('message sources expose list semantics, keyboard focus, and touch targets', () => {
  const source = read('../MessageSources.jsx')
  const webSources = source.slice(source.indexOf('{sources.map('))
  const msgContent = read('../MsgContent.jsx')
  const css = read('../ChatView.css')

  assert.match(source,
    /<section className="chat__sources" aria-label="Sources for this answer">/)
  assert.doesNotMatch(source, />Sources</,
    'source links should stand on their own at the end of the answer')
  assert.match(source, /<ul className="chat__sources-list">/)
  assert.match(msgContent,
    /msg\.role === 'assistant' && !isStreaming && \(\s*<MessageSources/,
    'source links should appear once the answer has settled, not move during streaming')
  assert.match(source,
    /<li key=\{source\.url\} className="chat__source-item chat__source-item--web">/)
  assert.match(source, /aria-label=\{`\$\{label\}.*opens in a new tab/)
  assert.match(webSources, /className="chat__source-icon" aria-hidden="true"/)
  assert.match(webSources, /\{sourceMark\(host\)\}/,
    'a recognisable local mark should not require a remote request')
  assert.doesNotMatch(webSources, /<img/,
    'message sources must not fetch cited sites from the reader browser')
  assert.doesNotMatch(webSources, /\/proxy|apiFetch|favicon/i,
    'merely viewing a citation should not make the server contact its site')
  assert.doesNotMatch(webSources, /chat__source-host/,
    'web source cards should prioritise the page title rather than repeat its URL host')
  assert.match(css, /\.chat__source-chip:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--accent\)/s)
  assert.match(css, /\.chat__source-chip\s*\{[^}]*border-radius:\s*999px/s)
  assert.match(css, /@media\s*\(pointer:\s*coarse\)\s*\{\s*\.chat__source-chip\s*\{\s*min-height:\s*44px/s)
})

test('the Memory search is a collapsed disclosure with linked result summaries', () => {
  const source = read('../MemoryRecallCard.jsx')
  const css = read('../ChatView.css')

  assert.match(source, /useDisclosureState\(chatId, disclosureKey\)/)
  assert.match(source, /aria-expanded=\{open\}/)
  assert.match(source, /hidden=\{!open\}/)
  assert.match(source, /<span className="chat__memory-kicker">Query<\/span>/)
  assert.match(source, /<span className="chat__memory-kicker">Results<\/span>/)
  assert.match(source, /<span className="chat__memory-kicker">Error<\/span>/)
  assert.match(source, /aria-label=\{`\$\{note\.label\} — open in Memory`\}/)
  assert.doesNotMatch(source, /Open Memory/,
    'only discovered note rows should navigate into Memory')
  assert.doesNotMatch(
    source,
    /chat__memory-note"[\s\S]{0,240}target="_blank"/,
    'a Memory note opens in the workspace, not a new browser tab',
  )
  assert.match(source, /onClick=\{event => openInternal\(/)
  assert.match(source, /event\.metaKey \|\| event\.ctrlKey \|\| event\.shiftKey \|\| event\.altKey/)
  assert.match(source, /Nothing relevant is recorded yet\./)
  assert.match(css, /@media\s*\(pointer:\s*coarse\)\s*\{\s*\.chat__memory-note\s*\{\s*min-height:\s*44px/s)
  assert.match(css, /\.chat__memory-note:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--accent\)/s)
})
