import test from 'node:test'
import assert from 'node:assert/strict'
import { liveProjectChat, projectChatStatus, projectChatTabLabel } from '../projectChats.js'

test('a working project chat shows its live title and Working… instead of New chat · Empty', () => {
  const stale = { id: 'c1', title: null, has_messages: false, archived_at: null }
  const live = { id: 'c1', title: 'Fix the cover page', has_messages: true, running: true, archived_at: null }
  const chat = liveProjectChat(stale, live)
  assert.equal(chat.title, 'Fix the cover page')
  assert.equal(projectChatStatus(chat), 'Working…')
})

test('without a live row the project row stands as fetched', () => {
  const chat = liveProjectChat({ id: 'c1', title: 'Draft', has_messages: false })
  assert.equal(chat.title, 'Draft')
  assert.equal(projectChatStatus(chat), 'Empty')
})

test('an idle chat with messages needs no status word; archive still shows', () => {
  assert.equal(projectChatStatus({ has_messages: true }), null)
  assert.equal(projectChatStatus({ has_messages: true, archived_at: '2026-10-27' }), 'Archived')
})

test('project chat tabs are named for the chat so truncation keeps the title', () => {
  assert.equal(projectChatTabLabel({ title: 'Fix the cover page', projectName: 'Thesis' }), 'Fix the cover page')
  assert.equal(projectChatTabLabel({ title: null, projectName: 'Thesis' }), 'Thesis · New chat')
})
