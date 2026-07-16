import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const inputBarSrc = readFileSync(new URL('../ChatInputBar.jsx', import.meta.url), 'utf8')
const chatViewSrc = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const changeHandler = inputBarSrc.match(
  /function handleTextareaChange\(e\) \{[\s\S]*?\n  \}/,
)?.[0] || ''

test('manual textarea changes remain enabled while voice input is active', () => {
  assert.doesNotMatch(changeHandler, /listeningRef\?\.current\) return/,
    'listening must not discard owner edits')
  assert.match(changeHandler,
    /if \(listeningRef\?\.current\) onManualVoiceEdit\?\.\(e\.target\.value\)/,
    'a live edit must rebase the recognition session before updating the draft')
  assert.match(changeHandler, /onInputChange\(e\.target\.value\)/,
    'the controlled composer must still receive the owner edit')
})

test('ChatView connects manual voice edits to the voice-input session', () => {
  assert.match(chatViewSrc, /onManualVoiceEdit=\{acceptManualEdit\}/)
})
