import assert from 'node:assert/strict'
import test from 'node:test'

import {
  CLAUDE_MODELS,
  CODEX_MODELS,
} from '../../ProviderModelPicker/ProviderModelPicker.jsx'

test('provider model fallbacks do not maintain a second display-name catalog', () => {
  for (const model of [...CLAUDE_MODELS, ...CODEX_MODELS]) {
    assert.equal(model.label, model.value)
  }
})
