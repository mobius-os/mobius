/* Copy links carry access only in fragments and never select an existing project. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { selectedCopyPaths, projectCopyDestination, readProjectCopyRequest, clearProjectCopyRequest, copyByteLabel, projectCopyRequest, readPublicProjectCopy, rememberProjectCopyRequest, consumeProjectCopyRequest } from '../projectCopies.js'

test('copy destination keeps source capability out of query strings and preserves deployment prefix', () => {
  const source = 'https://sender.example/project-copy#secret-token'
  const destination = new URL(projectCopyDestination('https://recipient.example/proxy/8001/shell/?old=x', source))
  assert.equal(destination.pathname, '/proxy/8001/shell/')
  assert.equal(destination.search, '')
  assert.equal(readProjectCopyRequest(destination.href), source)
  assert.equal(clearProjectCopyRequest(destination.href), 'https://recipient.example/proxy/8001/shell/')
})
test('copy destination rejects executable schemes and embedded credentials', () => {
  for (const address of ['javascript:alert(1)', 'file:///etc/passwd', 'https://owner:secret@example.com', 'not a URL']) {
    assert.throws(() => projectCopyDestination(address, 'https://sender.example/project-copy#token'))
  }
})
test('incoming links cannot reuse a project and missing copy fragments stay empty', () => {
  assert.equal(readProjectCopyRequest('https://recipient.example/shell/?project=existing'), '')
  assert.equal(readProjectCopyRequest('bad'), '')
  assert.equal(projectCopyDestination('http://localhost:8000/', 'https://sender.example/project-copy#new').split('#')[0], 'http://localhost:8000/shell/')
})
test('copy sizes remain readable for small and large projects', () => {
  assert.equal(copyByteLabel(50), '50 B')
  assert.equal(copyByteLabel(1024), '1.0 KB')
  assert.equal(copyByteLabel(1024*1024), '1.0 MB')
})

test('copy request uses the existing API prefix once and serializes reviewed selections', async () => {
  const originalFetch = globalThis.fetch
  let request
  globalThis.fetch = async (url, options) => { request = { url, options }; return new Response(JSON.stringify({ id: 'new-share' }), { status: 200 }) }
  try {
    assert.deepEqual(await projectCopyRequest('/projects/test/shares', { method: 'POST', body: { paths: ['README.md'], digest: 'reviewed' } }), { id: 'new-share' })
    assert.equal(request.url, '/api/project-copies/projects/test/shares')
    assert.deepEqual(JSON.parse(request.options.body), { paths: ['README.md'], digest: 'reviewed' })
  } finally { globalThis.fetch = originalFetch }
})

test('copy-link history presents UTC creation and expiry in the local timezone', async t => {
  const previousTimezone = process.env.TZ
  process.env.TZ = 'Europe/Paris'
  t.mock.method(Date, 'now', () => Date.UTC(2026, 10, 24))
  const client = new QueryClient()
  try {
    // The production formatter captures the local zone when imported.
    const { default: ProjectCopyPanel } = await import('../../components/Projects/ProjectCopyPanel.jsx')
    client.setQueryData(['project-copy', 'project', 'shares'], [{
      id: 'test-share',
      created_at: '2026-11-23T12:00:00',
      expires_at: '2026-11-30T12:00:00',
    }])
    const html = renderToStaticMarkup(createElement(QueryClientProvider, { client },
      createElement(ProjectCopyPanel, { project: { id: 'project' } })))
    assert.match(html, /Created 23rd Nov 2026, 13:00/)
    assert.match(html, /Expires: 30th Nov 2026, 13:00/)
  } finally {
    client.clear()
    if (previousTimezone === undefined) delete process.env.TZ
    else process.env.TZ = previousTimezone
  }
})

test('public copy metadata sends the capability in a body without owner credentials', async () => {
  const originalFetch = globalThis.fetch
  let request
  globalThis.fetch = async (url, options) => { request = { url, options }; return new Response(JSON.stringify({ name: 'Shared project' }), { status: 200 }) }
  try {
    await readPublicProjectCopy('share-secret')
    assert.equal(request.url, '/api/project-copies/metadata')
    assert.equal(request.options.credentials, 'omit')
    assert.equal(request.options.headers.Authorization, undefined)
    assert.deepEqual(JSON.parse(request.options.body), { token: 'share-secret' })
  } finally { globalThis.fetch = originalFetch }
})
test('stopped-link empty responses succeed and stale reviews surface status for re-review', async () => {
  const originalFetch = globalThis.fetch
  try {
    globalThis.fetch = async () => new Response(null, { status: 204 })
    assert.equal(await projectCopyRequest('/shares/one', { method: 'DELETE' }), null)
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'Files changed. Review again.' }), { status: 409 })
    await assert.rejects(projectCopyRequest('/projects/one/shares', { method: 'POST' }), error => error.status === 409 && error.message === 'Files changed. Review again.')
  } finally { globalThis.fetch = originalFetch }
})

test('copy intent crosses identity login in tab storage and is consumed exactly once', () => {
  const values = new Map()
  const session = { setItem: (key, value) => values.set(key, value), getItem: key => values.get(key), removeItem: key => values.delete(key) }
  const original = 'https://sender.example/project-copy#secret'
  const incoming = projectCopyDestination('https://recipient.example', original)
  assert.equal(rememberProjectCopyRequest(incoming, session), original)
  rememberProjectCopyRequest('https://recipient.example/shell/', session)
  assert.equal(values.size, 1)
  assert.equal(consumeProjectCopyRequest('https://recipient.example/shell/', session), original)
  assert.equal(consumeProjectCopyRequest('https://recipient.example/shell/', session), '')
})
test('new copy intent wins over remembered intent and storage failures retain local-login fragment', () => {
  const current = 'https://sender.example/project-copy#new'
  const incoming = projectCopyDestination('https://recipient.example', current)
  const session = { getItem: () => 'https://sender.example/project-copy#old', removeItem: () => {}, setItem: () => {} }
  assert.equal(consumeProjectCopyRequest(incoming, session), current)
  const unavailable = { getItem: () => { throw new Error('Unavailable') }, setItem: () => { throw new Error('Unavailable') } }
  assert.equal(rememberProjectCopyRequest(incoming, unavailable), current)
  assert.equal(consumeProjectCopyRequest(incoming, unavailable), current)
  assert.equal(consumeProjectCopyRequest('https://recipient.example/shell/', undefined), '')
})

test('copy selection is empty while loading and never carries choices into a different reviewed snapshot', () => {
  assert.deepEqual(selectedCopyPaths(null, undefined), [])
  const preview = { digest: 'new', files: [{path: 'README.md', selected: true}, {path: 'other.bin', selected: false}] }
  assert.deepEqual(selectedCopyPaths(null, preview), ['README.md'])
  assert.deepEqual(selectedCopyPaths({digest:'old', paths:['other.bin']}, preview), ['README.md'])
  assert.deepEqual(selectedCopyPaths({digest:'new', paths:[]}, preview), [])
  assert.deepEqual(selectedCopyPaths({digest:'new', paths:['other.bin']}, preview), ['other.bin'])
})
