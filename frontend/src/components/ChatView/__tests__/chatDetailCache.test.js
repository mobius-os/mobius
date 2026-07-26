import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  chatDetailCacheValue,
  chatEntryPhase,
} from '../../../lib/chatDetailCache.js'

test('a cached running chat paints immediately while catch-up runs', () => {
  assert.equal(chatEntryPhase({ messages: [], running: true }), 'cached')
  assert.equal(chatEntryPhase({ messages: [], running: false }), 'cached')
  assert.equal(chatEntryPhase(null), 'history')
})

test('prefetched chat detail matches the synchronous ChatView cache contract', () => {
  const source = {
    messages: [{
      role: 'assistant',
      blocks: [{ type: 'tool', status: 'running' }, { type: 'text', text: 'done' }],
    }],
    offset: 12,
    running: false,
    pending_messages: [{ id: 'queued' }],
    pending_question_id: 'question-1',
    provider: 'codex',
    created_by_app_id: 7,
    agent_settings_json: { model: 'example' },
    effective_agent_settings: { effort: 'high' },
    has_assistant_turns: true,
    auto_resume_on_limit: true,
    auto_resume_on_restart: false,
  }

  const cached = chatDetailCacheValue(source)

  assert.equal(cached.messages[0].blocks[0].status, 'done')
  assert.equal(source.messages[0].blocks[0].status, 'running', 'projection does not mutate the response')
  assert.equal(cached.offset, 12)
  assert.equal(cached.pending_question_id, 'question-1')
  assert.deepEqual(cached.chatInfo, {
    provider: 'codex',
    created_by_app_id: 7,
    agent_settings_json: { model: 'example' },
    effective: { effort: 'high' },
    has_assistant_turns: true,
    auto_resume_on_limit: true,
    auto_resume_on_restart: false,
  })
})
