import test from 'node:test'
import assert from 'node:assert/strict'
import { orderPickerModels, withRecentModel } from '../modelPickerOrder.js'

const rows = [
  { pid: 'codex', model: { id: 'codex-a' } },
  { pid: 'codex', model: { id: 'codex-b' } },
  { pid: 'claude', model: { id: 'claude-a' } },
]

test('recent models lead across providers, then preserve catalog order', () => {
  assert.deepEqual(
    orderPickerModels(rows, ['claude-a', 'codex-b', 'retired']).map(row => row.model.id),
    ['claude-a', 'codex-b', 'codex-a'],
  )
  assert.deepEqual(orderPickerModels(rows).map(row => row.model.id), rows.map(row => row.model.id))
})

test('confirmed choices move to front without changing hidden preferences', () => {
  assert.deepEqual(withRecentModel({ hidden_ids: ['codex-a'], recent_ids: ['codex-b', 'claude-a'] }, 'claude-a'), {
    hidden_ids: ['codex-a'], recent_ids: ['claude-a', 'codex-b'],
  })
  assert.equal(withRecentModel({ recent_ids: Array.from({ length: 20 }, (_, n) => String(n)) }, 'new').recent_ids.length, 20)
})
