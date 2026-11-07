/* Picker visibility saves name only the models that changed, so one editor can never overwrite another's changes, and a failed save shows the server's list again. */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { QueryClient } from '@tanstack/react-query'
import { hiddenChanges, modelQueries } from '../queries.js'

function client(hiddenIds) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  queryClient.setQueryData(modelQueries.prefs.key, { hidden_ids: hiddenIds })
  return queryClient
}

function serve(t, handler) {
  const sent = []
  t.mock.method(globalThis, 'fetch', async (url, init = {}) => {
    const request = { url: String(url), method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null }
    sent.push(request)
    return handler(request)
  })
  return sent
}

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

test('a switch sends only its own change and shows it before the server answers', async t => {
  const queryClient = client(['a', 'b'])
  let answer
  const sent = serve(t, () => new Promise(resolve => { answer = resolve }))
  const saving = modelQueries.prefs.saveChanges(queryClient, { a: false })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.deepEqual(queryClient.getQueryData(modelQueries.prefs.key), { hidden_ids: ['b'] })
  assert.equal(sent.length, 1)
  assert.equal(sent[0].method, 'PATCH')
  assert.match(sent[0].url, /\/api\/owner\/model-prefs$/)
  assert.deepEqual(sent[0].body, { hidden: { a: false } })
  answer(json({ hidden_ids: ['b', 'set-elsewhere'] }))
  assert.equal(await saving, true)
  // The server's whole list wins, including changes made on another page.
  assert.deepEqual(queryClient.getQueryData(modelQueries.prefs.key), { hidden_ids: ['b', 'set-elsewhere'] })
})

test('saves reach the server one at a time, in the order they were made', async t => {
  const queryClient = client([])
  const answers = []
  const sent = serve(t, () => new Promise(resolve => { answers.push(resolve) }))
  const first = modelQueries.prefs.saveChanges(queryClient, { a: true })
  const second = modelQueries.prefs.saveChanges(queryClient, { a: false })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(sent.length, 1)
  answers[0](json({ hidden_ids: ['a'] }))
  await first
  // An earlier answer never undoes the later change still on the wire.
  assert.deepEqual(queryClient.getQueryData(modelQueries.prefs.key), { hidden_ids: [] })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(sent.length, 2)
  assert.deepEqual(sent.map(request => request.body), [{ hidden: { a: true } }, { hidden: { a: false } }])
  answers[1](json({ hidden_ids: [] }))
  assert.equal(await second, true)
})

test('a failed save marks the list for a fresh read from the server', async t => {
  const queryClient = client(['a'])
  serve(t, () => json({ detail: 'nope' }, 500))
  assert.equal(await modelQueries.prefs.saveChanges(queryClient, { a: false }), false)
  // Any open editor refetches the owner's real list instead of keeping the guess.
  assert.equal(queryClient.getQueryState(modelQueries.prefs.key).isInvalidated, true)
})

test('Manage models saves the difference between the saved and edited lists', () => {
  assert.deepEqual(hiddenChanges(['a', 'b'], new Set(['b', 'c'])), { a: false, c: true })
  assert.deepEqual(hiddenChanges(['a'], new Set(['a'])), {})
})
