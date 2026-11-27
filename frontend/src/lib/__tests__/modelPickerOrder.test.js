import test from 'node:test'
import assert from 'node:assert/strict'
import { orderPickerModels } from '../modelPickerOrder.js'

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
