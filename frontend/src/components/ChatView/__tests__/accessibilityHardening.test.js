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

test('ShareAppSheet presents hosted use and installable copies as separate lifecycles', () => {
  const source = read('../../Drawer/ShareAppSheet.jsx')
  const client = read('../../../api/client.js')
  assert.match(source, /app\.hosted_publication/)
  assert.match(source, /onPublish\?\.\(app\.id\)/)
  assert.match(source, /onStop\?\.\(app\.id\)/)
  assert.match(source, />Install or remix</)
  assert.match(source, /editable copy/)
  assert.doesNotMatch(source, /public_enabled|onSetPublic/)
  assert.match(client, /\/hosted-publication.*method: 'PUT'/)
  assert.match(client, /\/hosted-publication.*method: 'DELETE'/)
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

test('message references are an accessible lazy disclosure with safe links', () => {
  const source = read('../MessageSources.jsx')
  const msgContent = read('../MsgContent.jsx')
  const css = read('../ChatView.css')
  const favicon = read('../SourceFavicon.jsx')

  assert.match(source, />References<\/span>/)
  assert.match(source, /aria-expanded=\{open\}/)
  assert.match(source, /aria-controls=\{bodyId\}/)
  assert.match(source, /hidden=\{!open\}/)
  assert.match(source, /\{open && loadedSources !== null && \(/,
    'reference links and favicons must not mount while collapsed')
  assert.match(source, /message-sources.*message_index=/s,
    'historical metadata should have a dedicated lazy read path')
  assert.match(source, /if \(!open \|\| loadedSources !== null/,
    'the metadata read must not begin before expansion')
  assert.match(source,
    /<ul className="chat__sources-list" aria-label="References for this answer">/)
  assert.match(msgContent,
    /msg\.role === 'assistant' && !isStreaming && \(\s*<MessageSources/,
    'the collapsed reference row should appear only after the answer settles')
  assert.match(msgContent, /sourceRef=\{msg\.source_ref\}/)
  assert.match(source,
    /<li key=\{source\.url\} className="chat__source-item chat__source-item--web">/)
  assert.match(source, /aria-label=\{`\$\{label\}.*opens in a new tab/)
  assert.match(source, /<SourceFavicon/,
    'expanded references should use the shared safe icon loader')
  assert.doesNotMatch(source, /<img/,
    'message sources must not fetch cited sites directly from the reader browser')
  assert.match(favicon,
    /apiFetch\(sourceFaviconProxyPath\(candidate\),\s*\{\s*timeoutMs:\s*FAVICON_TIMEOUT_MS/s,
    'every favicon candidate should use the authenticated server-side proxy')
  assert.match(favicon, /new IntersectionObserver/,
    'off-screen source icons should not trigger eager proxy requests')
  assert.match(favicon, /const pendingFavicons = new Map\(\)/,
    'repeated citations should share one in-flight proxy read')
  assert.doesNotMatch(source, /chat__source-host/,
    'web source cards should prioritise the page title rather than repeat its URL host')
  assert.match(css, /\.chat__source-chip:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--accent\)/s)
  assert.match(css, /\.chat__source-chip\s*\{[^}]*border-radius:\s*999px/s)
  assert.match(css,
    /@media\s*\(pointer:\s*coarse\)\s*\{\s*\.chat__sources-toggle,\s*\.chat__source-chip\s*\{\s*min-height:\s*44px/s,
    'both the disclosure and its links should keep full touch targets')
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
