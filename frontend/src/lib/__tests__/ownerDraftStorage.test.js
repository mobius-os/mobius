import test from 'node:test'
import assert from 'node:assert/strict'
import { clearOwnerDraftStorage } from '../ownerDraftStorage.js'


class MemoryStorage {
  constructor(values = {}) { this.values = new Map(Object.entries(values)) }
  get length() { return this.values.size }
  getItem(key) { return this.values.get(key) ?? null }
  setItem(key, value) { this.values.set(key, String(value)) }
  removeItem(key) { this.values.delete(key) }
  key(index) { return [...this.values.keys()][index] ?? null }
}


test('logout clears owner-authored drafts but keeps shell preferences', () => {
  const local = new MemoryStorage({
    'qa-draft:chat:q1': '{"answers":{}}',
    'mobius:persistent-drawer-open': 'true',
    token: 'owner-token',
  })
  const session = new MemoryStorage({
    'draft:chat': 'unfinished message',
    'draft-autosend:chat': '1',
    'pending-draft': 'new chat text',
    'pending-draft-autosend': '1',
    'chat-mode': 'follow',
  })

  clearOwnerDraftStorage([local, session])

  assert.equal(local.getItem('qa-draft:chat:q1'), null)
  assert.equal(session.getItem('draft:chat'), null)
  assert.equal(session.getItem('draft-autosend:chat'), null)
  assert.equal(session.getItem('pending-draft'), null)
  assert.equal(session.getItem('pending-draft-autosend'), null)
  assert.equal(local.getItem('mobius:persistent-drawer-open'), 'true')
  assert.equal(local.getItem('token'), 'owner-token')
  assert.equal(session.getItem('chat-mode'), 'follow')
})
