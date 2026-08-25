import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

/* These assertions read component source because the composer and the message
 * list have no DOM harness in this project, so their wiring to
 * markdownClipboard.js cannot be exercised behaviorally. They stay out of
 * markdownClipboard.test.js because the structural-test budget charges every
 * case in a file that reads source, and that file's coverage is behavioral.
 * Prefer deleting an assertion here over adding one: anything the module
 * itself can prove belongs in the behavioral file. */
test('assistant prose and the composer share the Markdown clipboard boundary', () => {
  const message = readFileSync(new URL('../MsgContent.jsx', import.meta.url), 'utf8')
  const composer = readFileSync(new URL('../ChatInputBar.jsx', import.meta.url), 'utf8')
  assert.match(message, /onCopy=\{\(event\) => copyAssistantSelection\(event, markdownForBlock\)\}/)
  assert.match(message, /data-assistant-markdown-block=/)
  assert.match(message, /if \(markdownByIndex\) return markdownByIndex\.get\(index\) \?\? ''/,
    'a partial cold render must not fall back to the hidden full message source')
  assert.match(composer, /assistantClipboardText\(\s*e\.clipboardData,\s*preferPlainText/)
  assert.match(composer, /pasteAsPlainTextRef\.current = isPlainTextPasteShortcut\(e\)/)
  assert.match(composer, /if \(queueClipboardTextUndoably\(/,
    'Markdown paste should enter the browser undo stack before using the controlled fallback')
  assert.match(composer, /pendingComposerCaretRef\.current = next/,
    'paste and sent-message history should share the controlled caret handoff')
  assert.doesNotMatch(composer, /setSelectionRange\(next\.caret/,
    'paste must not introduce a second requestAnimationFrame caret path')
})
