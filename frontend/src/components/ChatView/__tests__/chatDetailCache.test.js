import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  chatCacheEntryState,
  chatDetailCacheValue,
  chatSnapshotMatchesRuntime,
  mergeRecentMessagesIntoLoadedWindow,
  messageKey,
  messageMatchesKey,
  optimisticHandoffWindow,
  hasPendingQuestionMessage,
} from '../../../lib/chatDetailCache.js'

test('cache first paint requires the saved reading coordinate when one exists', () => {
  assert.equal(chatCacheEntryState(null), 'missing')
  assert.equal(chatCacheEntryState({ messages: [], offset: 0 }), 'missing',
    'legacy or previously poisoned cache shapes fail closed')
  assert.equal(chatCacheEntryState({
    restorationWindowComplete: true, messages: [], offset: 0,
  }), 'paintable',
    'a complete newly-created empty chat can paint before background refresh')
  const cached = {
    restorationWindowComplete: true,
    offset: 4,
    messages: [{ id: 'server-row', cid: 'client-row', role: 'user', ts: 10 }],
  }
  assert.equal(chatCacheEntryState(cached, 'server-row'), 'paintable')
  assert.equal(chatCacheEntryState(cached, 'client-row'), 'paintable',
    'optimistic-to-server aliases remain valid restoration coordinates')
  assert.equal(chatCacheEntryState(cached, 'user-10'), 'missing',
    'a role/timestamp alias waits for passive canonical remapping')
  assert.equal(chatCacheEntryState({
    restorationWindowComplete: true,
    offset: 4,
    messages: [{ id: 'persisted-answer', role: 'assistant', ts: 12 }],
  }, 'assistant-4'), 'paintable',
  'a live-first positional alias survives the authoritative timestamp')
  assert.equal(chatCacheEntryState(cached, 'server-row', true), 'validating',
    'a nested part mounts behind the gate for committed DOM validation')
  assert.equal(chatCacheEntryState(cached, 'missing-row', true), 'missing',
    'a nested part cannot validate when its row is absent')
  assert.equal(chatCacheEntryState(cached, 'missing-row'), 'missing',
    'an incomplete cache stays hidden until the anchor-addressed read settles')
})


test('a running cache waits for subscribe-time replay', () => {
  const running = {
    restorationWindowComplete: true,
    running: true,
    pending_question_id: null,
    offset: 0,
    messages: [{ id: 'owner-row', role: 'user', ts: 1 }],
  }
  assert.equal(chatCacheEntryState(running), 'stream-catchup')
  const parked = {
    ...running,
    pending_question_id: 'question-1',
    messages: [{
      role: 'assistant',
      blocks: [{ type: 'question', question_id: 'question-1', questions: [] }],
    }],
  }
  assert.equal(chatCacheEntryState(parked), 'paintable',
    'a parked question has no possible stream output')
  assert.equal(chatCacheEntryState({ ...parked, messages: running.messages }), 'missing',
    'a pending marker cannot make a cache paintable without its card')
  assert.equal(chatCacheEntryState(running, 'missing-row'), 'missing',
    'missing saved history remains stronger than the stream gate')
})

test('message row addresses remain stable across authoritative replacements', () => {
  assert.equal(messageKey({ id: 'message-1', role: 'user', ts: 10 }, 4), 'message-1')
  assert.equal(messageKey({ id: 7 }, 4), '7')
  assert.equal(messageKey({ cid: 'client-1', role: 'user', ts: 10 }, 4), 'client-1')
  assert.equal(messageKey({ role: 'assistant', ts: 10 }, 4), 'assistant-10')
  assert.equal(messageKey({ role: 'assistant' }, 4), 'assistant-4')
  const replaced = { id: 'server-1', cid: 'client-1', role: 'user', ts: 10 }
  assert.equal(messageMatchesKey(replaced, 4, 'server-1'), true)
  assert.equal(messageMatchesKey(replaced, 4, 'client-1'), true)
  assert.equal(messageMatchesKey(replaced, 4, 'user-10'), true)
  assert.equal(messageMatchesKey(replaced, 4, 'user-4'), true)
  assert.equal(messageMatchesKey(replaced, 4, 'assistant-10'), false)
})

test('prefetched chat detail matches the synchronous ChatView cache contract', () => {
  const source = {
    updated_at: '2026-07-30T12:00:00Z',
    messages: [{
      role: 'assistant',
      blocks: [{ type: 'tool', status: 'running' }, { type: 'text', text: 'done' }],
    }],
    offset: 12,
    total: 13,
    running: false,
    active_goal_objective: 'Finish the migration',
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

  assert.equal(cached.restorationWindowComplete, true)
  assert.equal(cached.messages[0].blocks[0].status, 'done')
  assert.equal(cached.updated_at, source.updated_at)
  assert.equal(source.messages[0].blocks[0].status, 'running', 'projection does not mutate the response')
  assert.equal(cached.offset, 12)
  assert.equal(cached.activeGoalObjective, 'Finish the migration')
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

test('malformed detail projections never gain first-paint provenance', () => {
  assert.equal(chatDetailCacheValue({
    messages: [], offset: 0, total: 0,
  }).restorationWindowComplete, true)
  assert.equal(chatDetailCacheValue({
    messages: [], offset: 0,
  }).restorationWindowComplete, false)
  assert.equal(chatDetailCacheValue({
    messages: [], total: 0,
  }).restorationWindowComplete, false)
  assert.equal(chatDetailCacheValue({
    offset: 0, total: 0,
  }).restorationWindowComplete, false)
  assert.equal(chatDetailCacheValue({
    messages: [{ id: 'tail' }], offset: -1, total: 1,
  }).restorationWindowComplete, false)
  assert.equal(chatDetailCacheValue({
    messages: [{ id: 'tail' }], offset: 2, total: 9,
  }).restorationWindowComplete, false)
})

test('optimistic handoff fills an empty cache without replacing a concurrent transcript', () => {
  const mounted = [{ id: 'local-row', optimistic: true }]
  assert.deepEqual(optimisticHandoffWindow({ messages: [], offset: 0 }, mounted, 4), {
    messages: mounted,
    offset: 4,
    restorationWindowComplete: true,
  })
  const concurrent = [{ id: 'concurrent-row' }]
  assert.deepEqual(optimisticHandoffWindow({
    messages: concurrent, offset: 7, restorationWindowComplete: true,
  }, mounted, 4), {
    messages: concurrent,
    offset: 7,
    restorationWindowComplete: true,
  })
  assert.equal(optimisticHandoffWindow({
    messages: concurrent, offset: 7,
  }, mounted, 4).restorationWindowComplete, false,
  'a legacy concurrent publication cannot inherit provenance from the server response')
})

test('a retained snapshot requires both the row version and pending card', () => {
  const updated_at = '2026-07-30T12:00:00Z'
  const question = {
    role: 'assistant',
    blocks: [{ type: 'question', question_id: 'question-1', questions: [] }],
  }
  const cached = { updated_at, messages: [question] }
  assert.equal(chatSnapshotMatchesRuntime(cached, {
    updated_at,
  }), true)
  assert.equal(chatSnapshotMatchesRuntime(cached, {
    updated_at: '2026-07-30T12:00:01Z',
  }), false)
  assert.equal(chatSnapshotMatchesRuntime(cached, {}), false)
  assert.equal(chatSnapshotMatchesRuntime({}, {
    updated_at,
  }), false)
  assert.equal(chatSnapshotMatchesRuntime(cached, {
    updated_at,
    pending_question_id: 'question-1',
  }), true)
  assert.equal(chatSnapshotMatchesRuntime({ ...cached, messages: [] }, {
    updated_at,
    pending_question_id: 'question-1',
  }), false)
  assert.equal(chatSnapshotMatchesRuntime({
    ...cached,
    messages: [{
      ...question,
      blocks: [{ ...question.blocks[0], answers: { pick: 'yes' } }],
    }],
  }, {
    updated_at,
    pending_question_id: 'question-1',
  }), false)
  assert.equal(chatSnapshotMatchesRuntime(cached, {
    updated_at,
    pending_question_id: 'question-2',
  }), false)
})

test('pending question lookup requires the exact unanswered owner row', () => {
  const messages = [
    { role: 'user', content: 'choose' },
    {
      role: 'assistant',
      blocks: [{ type: 'question', question_id: 'question-1', questions: [] }],
    },
  ]
  assert.equal(hasPendingQuestionMessage(messages, 'question-1'), true)
  assert.equal(hasPendingQuestionMessage(messages, 'question-2'), false)
  assert.equal(hasPendingQuestionMessage([], 'question-1'), false)
  assert.equal(hasPendingQuestionMessage([{
    ...messages[1],
    blocks: [{ ...messages[1].blocks[0], answers: { pick: 'yes' } }],
  }], 'question-1'), false)
})

test('a tail refresh retains every verified older row needed by a saved address', () => {
  const loaded = Array.from({ length: 40 }, (_, index) => ({
    id: `message-${index + 5}`,
    content: `Loaded ${index + 5}`,
  }))
  const recent = Array.from({ length: 20 }, (_, index) => ({
    id: `message-${index + 25}`,
    content: `Fresh ${index + 25}`,
  }))
  const merged = mergeRecentMessagesIntoLoadedWindow({
    loadedMessages: loaded,
    loadedOffset: 5,
    recentMessages: recent,
    recentOffset: 25,
  })
  assert.equal(merged.offset, 5)
  assert.equal(merged.messages.length, 40)
  assert.equal(merged.messages[0].content, 'Loaded 5')
  assert.equal(merged.messages[20].content, 'Fresh 25')
})
