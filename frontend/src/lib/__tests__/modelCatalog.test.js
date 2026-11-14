import test from 'node:test'
import assert from 'node:assert/strict'
import {
  catalogDefault,
  catalogProviders,
  selectedCatalogModel,
} from '../modelCatalog.js'

const catalog = {
  providers: {
    mobius: [{ id: 'flash', label: 'Flash' }, { id: 'deep', label: 'Deep' }],
    codex: [{ id: 'codex-model' }],
  },
  defaults: { mobius: 'flash' },
  replacements: { mobius: { evolve: 'flash', reflect: 'deep' } },
}

test('live defaults and retired selections resolve without altering saved ids', () => {
  assert.equal(catalogDefault(catalog, 'mobius'), 'flash')
  assert.equal(catalogDefault(catalog, 'codex'), '')
  assert.equal(selectedCatalogModel(catalog, 'mobius', 'evolve'), 'flash')
  assert.equal(selectedCatalogModel(catalog, 'mobius', 'reflect'), 'deep')
  assert.equal(selectedCatalogModel(catalog, 'codex', 'evolve'), 'evolve')
  assert.equal(selectedCatalogModel(catalog, 'mobius', 'unknown'), 'unknown')
  assert.equal(catalog.providers.mobius.some(row => row.id === 'evolve'), false)
})

test('invalid replacement targets never masquerade as selectable rows', () => {
  const invalid = { ...catalog, replacements: { mobius: { evolve: 'missing' } } }
  assert.equal(selectedCatalogModel(invalid, 'mobius', 'evolve'), 'evolve')
  assert.deepEqual(catalogProviders(catalog), catalog.providers)
  assert.deepEqual(catalogProviders({ mobius: [] }), { mobius: [] })
})
