import test from 'node:test'
import assert from 'node:assert/strict'

import { copyableMessageText } from '../messageCopy.js'

test('copyableMessageText copies visible prose and excludes tool chrome', () => {
  assert.equal(copyableMessageText({
    role: 'assistant',
    blocks: [
      { type: 'text', content: 'First paragraph.' },
      { type: 'tool', tool: 'Bash', input: 'secret command' },
      { type: 'text', content: 'Second paragraph.' },
    ],
  }), 'First paragraph.\n\nSecond paragraph.')
})

test('copyableMessageText removes hidden user augmentation', () => {
  assert.equal(copyableMessageText({
    role: 'user',
    content: 'Visible request\n\n<agent_experience>hidden context</agent_experience>',
  }), 'Visible request')
})

test('copyableMessageText ignores hidden transcript rows', () => {
  assert.equal(copyableMessageText({ hidden: true, content: 'do not copy' }), '')
})

test('copyableMessageText ignores automatic continuation product markers', () => {
  assert.equal(copyableMessageText({
    role: 'user',
    kind: 'auto_continuation',
    content: 'continue',
  }), '')
})
