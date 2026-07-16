import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  getToken,
  isAppScopedToken,
  setEmbeddedToken,
} from '../../api/client.js'

function token(payload) {
  const encoded = Buffer.from(JSON.stringify(payload)).toString('base64url')
  return `header.${encoded}.signature`
}

test('embedded credential accepts only an app-scoped token and stays in memory', () => {
  const appToken = token({ scope: 'app', app_id: 59, app_nonce: 'instance' })
  assert.equal(isAppScopedToken(appToken), true)
  assert.equal(setEmbeddedToken(appToken), true)
  assert.equal(getToken(), appToken)
  setEmbeddedToken(null)
})

test('embedded credential rejects owner, malformed, and unbound app tokens', () => {
  const ownerToken = token({ sub: 'owner' })
  const unboundAppToken = token({ scope: 'app' })
  assert.equal(isAppScopedToken(ownerToken), false)
  assert.equal(isAppScopedToken(unboundAppToken), false)
  assert.equal(isAppScopedToken('not-a-jwt'), false)
  assert.equal(setEmbeddedToken(ownerToken), false)
})
