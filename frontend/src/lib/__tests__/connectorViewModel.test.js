import test from 'node:test'
import assert from 'node:assert/strict'

import {
  connectorSchemaCostLabel,
  connectorStatus,
} from '../connectorViewModel.js'

test('connector schema cost states the neutral catalog size without loading claims', () => {
  assert.equal(connectorSchemaCostLabel(0), '')
  assert.equal(connectorSchemaCostLabel(420), '~420 tool-schema tokens')
  assert.equal(connectorSchemaCostLabel(1250), '~1.3k tool-schema tokens')
  assert.equal(connectorSchemaCostLabel(12500), '~13k tool-schema tokens')
})

test('connector status keeps reachability distinct from the owner toggle', () => {
  assert.deepEqual(connectorStatus({ enabled: true, status: 'ok' }), {
    color: '--green', text: 'Available',
  })
  assert.deepEqual(connectorStatus({ enabled: false, status: 'ok' }), {
    color: '--border', text: 'Off',
  })
  assert.deepEqual(connectorStatus({ enabled: false, status: 'error' }), {
    color: '--danger', text: 'Needs attention',
  })
})
