import test from 'node:test'
import assert from 'node:assert/strict'
import { modelEfforts, validEffort } from '../../ui/modelEfforts.js'

const providerEfforts = [
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
]

test('modelEfforts keeps provider defaults for legacy registry rows', () => {
  assert.equal(modelEfforts(providerEfforts, { id: 'legacy' }), providerEfforts)
})

test('modelEfforts honors model-specific levels and future values', () => {
  assert.deepEqual(
    modelEfforts(providerEfforts, {
      id: 'future',
      effort_levels: ['low', 'high', { value: 'max', label: 'Maximum' }],
    }),
    [
      { value: 'low', label: 'Low' },
      { value: 'high', label: 'High' },
      { value: 'max', label: 'Maximum' },
    ],
  )
})

test('validEffort replaces a level unsupported by the selected model', () => {
  assert.equal(validEffort(providerEfforts, 'max'), 'medium')
  assert.equal(validEffort(providerEfforts, 'high'), 'high')
})
