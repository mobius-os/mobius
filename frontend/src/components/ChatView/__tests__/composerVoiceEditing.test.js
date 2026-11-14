import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const inputBarSrc = readFileSync(new URL('../ChatInputBar.jsx', import.meta.url), 'utf8')
const editorSrc = readFileSync(new URL('../MarkdownComposer.jsx', import.meta.url), 'utf8')
const chatViewSrc = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const changeHandler = inputBarSrc.match(
  /function handleComposerChange\(value\) \{[\s\S]*?\n  \}/,
)?.[0] || ''
const historyValueHandler = inputBarSrc.match(
  /function showComposerValue\(value\) \{[\s\S]*?\n  \}/,
)?.[0] || ''
const slashCommandHandler = inputBarSrc.match(
  /function acceptSlashCommand\(command\) \{[\s\S]*?\n  \}/,
)?.[0] || ''

test('manual composer changes remain enabled while voice input is active', () => {
  assert.doesNotMatch(changeHandler, /listeningRef\?\.current\) return/,
    'listening must not discard owner edits')
  assert.match(changeHandler,
    /if \(listeningRef\?\.current\) onManualVoiceEdit\?\.\(value\)/,
    'a live edit must rebase the recognition session before updating the draft')
  assert.match(changeHandler, /onInputChange\(value\)/,
    'the controlled composer must still receive the owner edit')
  assert.ok(
    editorSrc.indexOf('props.current.onChange?.(transactions.at(-1).state.doc.toString())')
      < editorSrc.indexOf("props.current.onInputIntent?.({ type: 'input'"),
    'scroll ownership changes only after the controlled draft and voice baseline accept the edit',
  )
})

test('ChatView connects manual voice edits to the voice-input session', () => {
  assert.match(chatViewSrc, /onManualVoiceEdit=\{acceptManualEdit\}/)
})

test('history recall and the returning draft rebase live dictation before changing the box', () => {
  assert.match(historyValueHandler,
    /if \(listeningRef\?\.current\) \{[\s\S]*?onManualVoiceEdit\?\.\(value\)/,
    'history recall must take the same voice-session ownership as typed edits')
  assert.ok(
    historyValueHandler.indexOf('onManualVoiceEdit?.(value)')
      < historyValueHandler.indexOf('onInputChange(value)'),
    'the voice baseline must update before the controlled composer value',
  )
})

test('accepting a slash command rebases live dictation before changing the draft', () => {
  assert.match(slashCommandHandler,
    /if \(listeningRef\?\.current\) onManualVoiceEdit\?\.\(value\)/,
    'command acceptance must retire the in-flight speech result before updating the draft')
  assert.ok(
    slashCommandHandler.indexOf('onManualVoiceEdit?.(value)')
      < slashCommandHandler.indexOf('onInputChange(value)'),
    'the voice baseline must update before the controlled composer value',
  )
  assert.match(slashCommandHandler,
    /requestAnimationFrame\(\(\) => placeCaretAtTextEnd\(inputRef\?\.current\)\)/,
    'command acceptance must place the caret after the controlled value commits')
})
