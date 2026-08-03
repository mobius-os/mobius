import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const inputBarSrc = readFileSync(new URL('../ChatInputBar.jsx', import.meta.url), 'utf8')
const chatViewSrc = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const changeHandler = inputBarSrc.match(
  /function handleTextareaChange\(e\) \{[\s\S]*?\n  \}/,
)?.[0] || ''
const historyMoveHandler = inputBarSrc.match(
  /function applyHistoryMove\(historyMove\) \{[\s\S]*?\n    \}/,
)?.[0] || ''
const slashCommandHandler = inputBarSrc.match(
  /function acceptSlashCommand\(command\) \{[\s\S]*?\n  \}/,
)?.[0] || ''

test('manual textarea changes remain enabled while voice input is active', () => {
  assert.doesNotMatch(changeHandler, /listeningRef\?\.current\) return/,
    'listening must not discard owner edits')
  assert.match(changeHandler,
    /if \(listeningRef\?\.current\) onManualVoiceEdit\?\.\(value\)/,
    'a live edit must rebase the recognition session before updating the draft')
  assert.match(changeHandler, /onInputChange\(value\)/,
    'the controlled composer must still receive the owner edit')
  assert.ok(
    changeHandler.indexOf('onInputChange(value)')
      < changeHandler.indexOf('onInputIntent?.(e.nativeEvent)'),
    'scroll ownership may change only after React accepts the controlled draft',
  )
})

test('ChatView connects manual voice edits to the voice-input session', () => {
  assert.match(chatViewSrc, /onManualVoiceEdit=\{acceptManualEdit\}/)
})

test('sent-message recall rebases live dictation before changing the draft', () => {
  assert.match(historyMoveHandler,
    /if \(listeningRef\?\.current\) \{[\s\S]*?onManualVoiceEdit\?\.\(historyMove\.value\)/,
    'history recall must take the same voice-session ownership as typed edits')
  assert.ok(
    historyMoveHandler.indexOf('onManualVoiceEdit?.(historyMove.value)')
      < historyMoveHandler.indexOf('onInputChange(historyMove.value)'),
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
})
