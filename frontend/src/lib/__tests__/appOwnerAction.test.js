import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createAppOwnerActionProvider } from '../appOwnerAction.js'
const response = value => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
const tick = () => new Promise(resolve => setImmediate(resolve))
function fixture() {
  const calls = [], events = [], forms = []
  const request = {
    async prepare(...args) { calls.push(['prepare', ...args]); return response({ ticket: 'one-use', title: 'Trusted title', fields: [{ name: 'key' }] }) },
    async submit(...args) { calls.push(['submit', args[0], args[1]]); return response({ status: 'completed', message: 'Done' }) },
    async cancel(...args) { calls.push(['cancel', ...args]); return response({ status: 'cancelled' }) },
  }
  const provider = createAppOwnerActionProvider({ appId: 24, present: form => forms.push(form), request })
  const channel = { ready: x => events.push(['ready', x]), result: x => events.push(['result', x]), error: x => events.push(['error', x.message]) }
  return { calls, events, forms, provider, channel, request }
}
test('only trusted UI supplies values; frame gets fixed outcome only', async () => {
  const f = fixture(); f.provider.open({ input: { action: 'connect', context: { item: 'notes' } }, channel: f.channel })
  await tick(); assert.deepEqual(f.calls.map(x => x[0]), ['prepare'])
  const values = { key: 'synthetic-secret' }; await f.forms.at(-1).submit(values)
  assert.equal(values.key, '')
  assert.equal(f.calls[1][1], 24)
  assert.equal(f.events.at(-1)[0], 'result')
  assert.ok(!JSON.stringify(f.events).includes('synthetic-secret'))
})
test('cancellation while preparing never leaves a stale form', async () => {
  const f = fixture(); let release
  f.request.prepare = () => new Promise(resolve => { release = resolve })
  const session = f.provider.open({ input: { action: 'connect' }, channel: f.channel })
  session.control('cancel'); release(response({ ticket: 'late', fields: [] })); await tick()
  assert.ok(f.forms.every(x => x === null)); assert.equal(f.calls[0][0], 'cancel')
})
test('frame controls cannot submit or confirm an operation', async () => {
  const f = fixture(); const session = f.provider.open({ input: { action: 'connect' }, channel: f.channel })
  await tick(); session.control('submit', { key: 'synthetic' }); session.control('finish')
  assert.deepEqual(f.calls.map(x => x[0]), ['prepare'])
  session.control('cancel'); assert.equal(f.calls[1][0], 'cancel')
})
test('arbitrary frame commands and credential fields are rejected', () => {
  const f = fixture()
  for (const input of [{ action: '../connect' }, { action: 'connect', command: 'python' }, { action: 'connect', fields: { key: 'synthetic' } }]) {
    assert.throws(() => f.provider.open({ input, channel: f.channel }), TypeError)
  }
  assert.equal(f.calls.length, 0)
})
test('submission is single flight and failure is not retried', async () => {
  const f = fixture(); let count = 0
  f.request.submit = async () => { count++; throw new Error('Do not echo server failure') }
  f.provider.open({ input: { action: 'connect' }, channel: f.channel }); await tick()
  const form = f.forms.at(-1); await Promise.all([form.submit({ key: 'one' }), form.submit({ key: 'two' })])
  assert.equal(count, 1); assert.ok(!JSON.stringify(f.events).includes('server failure'))
})
