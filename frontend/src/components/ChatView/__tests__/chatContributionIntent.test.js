import test from 'node:test'
import assert from 'node:assert/strict'

import {
  CHAT_CONTRIBUTION_PREPARE_PROMPT,
  chatContributionPrepareAction,
  chatContributionPrepareSubmission,
} from '../chatContributionIntent.js'

test('chat preparation is private, scoped to recorded edits, and leaves publishing separate', () => {
  assert.match(CHAT_CONTRIBUTION_PREPARE_PROMPT, /file changes recorded by this chat/)
  assert.match(CHAT_CONTRIBUTION_PREPARE_PROMPT, /Verify the current source state/)
  assert.match(CHAT_CONTRIBUTION_PREPARE_PROMPT, /privately prepare every worthwhile contribution/)
  assert.match(CHAT_CONTRIBUTION_PREPARE_PROMPT, /Do not push, publish, or send anything upstream/)
})

test('an active reply makes the preparation action honestly queue itself', () => {
  assert.equal(chatContributionPrepareAction(false).label, 'Prepare contributions')
  assert.equal(chatContributionPrepareAction(true).label, 'Queue preparation')
})

test('the chat action sends independently without consuming the owner draft or attachments', () => {
  assert.deepEqual(chatContributionPrepareSubmission(), {
    text: CHAT_CONTRIBUTION_PREPARE_PROMPT,
    options: { attachments: [], preserveComposer: true },
  })
})
