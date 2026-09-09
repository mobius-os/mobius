/* Project preparation uses Contribute admission without any publication call. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { prepareProjectContribution } from '../projectContribution.js'

const project = { id: 'project-one', name: 'Sample', template: { imported_from: { management: 'linked', kind: 'app', id: 7 } } }
const response = value => new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } })
function fixture({ apps = [{ id: 12, slug: 'contribute' }], head = 'abc', cursor = 9, connected = true, outcome = 'started' } = {}) {
  const calls = []
  return {
    calls,
    api: {
      apps: { list: async () => response(apps) },
      projects: {
        remoteStatus: async () => response({ connected, repository: 'example/sample', head, dirty: true, github_connected: false }),
        changes: async () => response({ cursor }),
      },
      auth: { provider: { appToken: async id => { calls.push(['token', id]); return response({ token: 'scoped-test-authorization' }) } } },
      appChats: { startWithToken: async (token, body) => { calls.push(['start', token, body]); return response({ chat_id: 'private-review', outcome }) } },
    },
  }
}

test('one owner click starts a visible Contribute-owned private review without requiring public sign-in', async () => {
  const f = fixture()
  const chat = await prepareProjectContribution(f.api, project, 'intent-one')
  assert.equal(chat.id, 'private-review')
  assert.equal(chat.reused, false)
  assert.deepEqual(f.calls[0], ['token', 12])
  const body = f.calls[1][2]
  assert.equal(body.owner_visible, true)
  assert.equal(body.scope, 'project-prepare:project-one:intent-one')
  assert.match(body.content, /source only, not all local projects/)
  assert.match(body.content, /preserve their source-chat provenance/)
  assert.match(body.content, /Send PR remains a separate explicit owner decision/)
  assert.equal(f.calls.length, 2)
})

test('retry keeps the original admission while a new deliberate preparation gets a fresh scope', async () => {
  const a = fixture({ outcome: 'reused' })
  assert.equal((await prepareProjectContribution(a.api, project, 'intent-one')).reused, true)
  const b = fixture({ cursor: 10 })
  await prepareProjectContribution(b.api, project, 'intent-two')
  assert.notEqual(a.calls[1][2].scope, b.calls[1][2].scope)
})

test('missing owning adapter, Contribute or repository never starts an agent', async () => {
  const unsupported = fixture()
  await assert.rejects(prepareProjectContribution(unsupported.api, { ...project, template: {} }, 'intent-one'), /linked to an installed app/)
  assert.deepEqual(unsupported.calls, [])
  for (const [options, expected] of [[{ apps: [] }, /Install Contribute/], [{ connected: false }, /Connect this Project/]]) {
    const f = fixture(options)
    await assert.rejects(prepareProjectContribution(f.api, project, 'intent-one'), expected)
    assert.deepEqual(f.calls, [])
  }
})

test('failed admission remains visible and is never reported as prepared', async () => {
  const f = fixture()
  f.api.appChats.startWithToken = async () => response({})
  await assert.rejects(prepareProjectContribution(f.api, project, 'intent-one'), /did not return a conversation/)
})
