/* Picker visibility saves name only the models that changed, so one editor can never overwrite another's changes, and a failed save shows the server's list again. */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { QueryClient } from '@tanstack/react-query'
import { modelQueries } from '../queries.js'

function client(hiddenIds) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  queryClient.setQueryData(modelQueries.prefs.key, { hidden_ids: hiddenIds })
  return queryClient
}

function serve(t, handler) {
  const sent = []
  t.mock.method(globalThis, 'fetch', async (url, init = {}) => {
    const request = { url: String(url), method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null, signal: init.signal }
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
  assert.deepEqual(queryClient.getQueryData(modelQueries.prefs.key), { hidden_ids: ['b', 'set-elsewhere'], recent_ids: [] })
})

test('visibility reply cannot undo a model picked while the save was in flight', async t => {
  const queryClient = client(['a'])
  let answer
  serve(t, () => new Promise(resolve => { answer = resolve }))
  const saving = modelQueries.prefs.saveChanges(queryClient, { a: false })
  await new Promise(resolve => setTimeout(resolve, 0))
  queryClient.setQueryData(modelQueries.prefs.key, previous => ({
    ...previous, recent_ids: ['new-model'],
  }))
  answer(json({ hidden_ids: [], recent_ids: ['old-model'] }))
  assert.equal(await saving, true)
  assert.deepEqual(queryClient.getQueryData(modelQueries.prefs.key), {
    hidden_ids: [], recent_ids: ['new-model'],
  })
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

test('a stuck save gives up after its deadline, so later saves still reach the server', async t => {
  const queryClient = client([])
  const sent = serve(t, request => request.body?.hidden.a
    ? new Promise((_, reject) => request.signal.addEventListener('abort', () => reject(request.signal.reason)))
    : json({ hidden_ids: [] }))
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const stuck = modelQueries.prefs.saveChanges(queryClient, { a: true })
  const later = modelQueries.prefs.saveChanges(queryClient, { a: false })
  for (let i = 0; i < 5; i += 1) await Promise.resolve()
  assert.equal(sent.length, 1)
  t.mock.timers.tick(15_000)
  assert.equal(await stuck, false)
  assert.equal(await later, true)
  // (A timed-out request also asks the connectivity probe to check the server.)
  const saves = sent.filter(request => request.method === 'PATCH')
  assert.deepEqual(saves.map(request => request.body), [{ hidden: { a: true } }, { hidden: { a: false } }])
  assert.deepEqual(queryClient.getQueryData(modelQueries.prefs.key), { hidden_ids: [], recent_ids: [] })
  // The timed-out save may still land after the later one: read the server's list again.
  assert.equal(queryClient.getQueryState(modelQueries.prefs.key).isInvalidated, true)
})

test('a save the server answered needs no extra read', async t => {
  const queryClient = client(['a'])
  serve(t, () => json({ hidden_ids: [] }))
  assert.equal(await modelQueries.prefs.saveChanges(queryClient, { a: false }), true)
  assert.equal(queryClient.getQueryState(modelQueries.prefs.key).isInvalidated, false)
})


test('a new picker reads the latest agent even inside the shared cache freshness window', async t => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 30_000 } } })
  queryClient.setQueryData(modelQueries.prefs.key, {
    hidden_ids: [], recent_ids: ['old-model'],
    selected_agent: { provider: 'codex', model: 'old-model', effort: 'low' },
  })
  const selected = { provider: 'claude', model: 'latest-model', effort: 'high' }
  const sent = serve(t, () => json({ hidden_ids: [], recent_ids: ['latest-model'], selected_agent: selected }))
  try {
    const prefs = await queryClient.fetchQuery(modelQueries.prefs.options())
    assert.equal(sent.length, 1)
    assert.deepEqual(prefs.selected_agent, selected)
  } finally {
    queryClient.clear()
  }
})
