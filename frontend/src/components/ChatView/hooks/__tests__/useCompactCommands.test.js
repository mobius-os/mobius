/* Compaction readback never owns action latches or crosses a visible chat lifecycle. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { api } from '../../../../api/client.js'
import useCompactCommands from '../useCompactCommands.js'
import { renderHook } from './react-hook-shim.mjs'

const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve() }
const response = body => Response.json(body)
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function setup(t) {
  const reads = [], posts = [], stops = [], drafts = [], failures = [], messages = []
  for (const name of ['compact', 'compactStop', 'compactProgress']) {
    const original = api.chats[name]
    t.after(() => { api.chats[name] = original })
  }
  api.chats.compactProgress = (chatId, options) => {
    const pending = deferred()
    reads.push({ chatId, options, ...pending })
    return pending.promise
  }
  api.chats.compact = (chatId, body) => {
    const pending = deferred()
    posts.push({ chatId, body, ...pending })
    return pending.promise
  }
  api.chats.compactStop = chatId => {
    const pending = deferred()
    stops.push({ chatId, ...pending })
    return pending.promise
  }
  const props = {
    chatId: 'a', provisionalNewChat: false, hidden: false, serverCompactingKind: null,
    activationSettledRef: { current: true }, inputValueRef: { current: '' },
    setComposerInput: text => drafts.push(text), setSendFailure: error => failures.push(error),
    fetchMessages: async options => messages.push(options),
  }
  const hook = renderHook(useCompactCommands, props)
  t.after(() => hook.unmount())
  return { hook, props, reads, posts, stops, drafts, failures, messages }
}

test('resolved compact POST releases busy and admission while optional status GET stays deferred', async t => {
  const { hook, reads, posts, drafts, messages } = setup(t)
  const first = hook.result.current.runCompactCommand('Keep decisions', '/compact Keep decisions')
  assert.equal(hook.result.current.compactingChat, true)
  await hook.result.current.runCompactCommand()
  assert.equal(posts.length, 1, 'duplicate action during POST is blocked')
  posts[0].resolve(response({ ok: true, progress: { state: 'completed' } }))
  await first
  assert.equal(hook.result.current.compactingChat, false)
  assert.equal(hook.result.current.compactingChatTargetRef.current, null)
  assert.equal(reads.length, 2)
  assert.equal(reads[0].options.signal.aborted, true, 'superseded entry GET canceled')
  assert.equal(reads[1].options.signal.aborted, false)
  assert.deepEqual(messages, [{ force: true }])
  assert.deepEqual(drafts, [''])
  const second = hook.result.current.runCompactCommand('', null, 'recovery-a')
  assert.equal(posts.length, 2, 'deferred readback cannot block another explicit action')
  assert.equal(posts[1].body.recovery_id, 'recovery-a')
  assert.deepEqual(drafts, [''], 'card action preserves draft')
  reads[1].reject(new Error('status failed'))
  await flush()
  assert.equal(hook.result.current.compactProgressRecord.progress.state, 'completed')
  posts[1].resolve(response({ progress: { state: 'paused', recovery_id: 'recovery-a' } }))
  await second
  assert.equal(posts.length, 2, 'readback never auto-continues')
})

test('Pause POST releases its admission latch independently of deferred and failed status GETs', async t => {
  const { hook, reads, stops, failures } = setup(t)
  const first = hook.result.current.stopCompactCommand()
  await hook.result.current.stopCompactCommand()
  assert.equal(stops.length, 1)
  stops[0].resolve(response({ ok: true }))
  await first
  const second = hook.result.current.stopCompactCommand()
  assert.equal(stops.length, 2)
  reads[1].reject(new Error('readback unavailable'))
  await flush()
  assert.deepEqual(failures, [], 'optional GET failure is not an action failure')
  stops[1].resolve(response({ ok: true }))
  await second
  const third = hook.result.current.stopCompactCommand()
  assert.equal(stops.length, 3)
  stops[2].resolve(response({ ok: true }))
  await third
})

test('failed compact restores the submitted slash command without losing a newer draft or recovery handle', async t => {
  const { hook, props, reads, posts, drafts, failures } = setup(t)
  reads[0].resolve(response({ progress: { state: 'paused', recovery_id: 'saved' } }))
  await flush()
  const action = hook.result.current.runCompactCommand('', '/compact')
  props.inputValueRef.current = 'new owner draft'
  posts[0].reject(new Error('POST failed'))
  await action
  assert.equal(hook.result.current.compactingChat, false)
  assert.equal(drafts.at(-1), 'new owner draft', 'restoration does not overwrite newer input')
  assert.match(failures.at(-1), /couldn’t send/)
  reads[1].reject(new Error('GET failed'))
  await flush()
  assert.equal(hook.result.current.compactProgressRecord.progress.recovery_id, 'saved')
})

test('chat switch cancels status GET and fences late GET/POST errors, draft restoration, and readback', async t => {
  const { hook, props, reads, posts, drafts, failures, messages } = setup(t)
  const action = hook.result.current.runCompactCommand()
  hook.rerender({ ...props, chatId: 'b' })
  assert.equal(reads[0].options.signal.aborted, true)
  reads[0].resolve(response({ progress: { state: 'paused', recovery_id: 'old' } }))
  reads[1].resolve(response({ progress: { state: 'paused', recovery_id: 'new' } }))
  posts[0].reject(new Error('old POST failed'))
  await action
  await flush()
  assert.equal(hook.result.current.compactProgressRecord.chatId, 'b')
  assert.equal(hook.result.current.compactProgressRecord.progress.recovery_id, 'new')
  assert.deepEqual(drafts, [''])
  assert.deepEqual(failures, [null])
  assert.deepEqual(messages, [])
  assert.equal(reads.length, 2, 'old action finally cannot read either chat again')
})

test('hide/unmount cancels reads and late POST completion cannot resurrect optional GETs', async t => {
  const { hook, props, reads, posts, stops } = setup(t)
  const action = hook.result.current.runCompactCommand('', null)
  hook.rerender({ ...props, hidden: true })
  assert.equal(reads[0].options.signal.aborted, true)
  posts[0].resolve(response({ progress: { state: 'paused' } }))
  await action
  assert.equal(reads.length, 1)
  hook.rerender(props)
  assert.equal(reads.length, 2)
  const stop = hook.result.current.stopCompactCommand()
  hook.unmount()
  assert.equal(reads[1].options.signal.aborted, true)
  stops[0].resolve(response({ ok: true }))
  await stop
  reads[1].resolve(response({ progress: { state: 'stale' } }))
  await flush()
  assert.equal(reads.length, 2, 'unmounted action cannot resurrect a GET')
})


test('progress GET deadline aborts the actual transport without erasing recovery or restarting work', async t => {
  const { hook, props, reads, posts } = setup(t)
  reads[0].resolve(response({ progress: { state: 'paused', recovery_id: 'saved' } }))
  await flush()
  // Restore the real API read to prove timeout and lifecycle signals compose.
  // setup registered restoration before this fixture replacement.
  const { apiFetch } = await import('../../../../api/client.js')
  api.chats.compactProgress = (chatId, options) => apiFetch(`/chats/${chatId}/compact-progress`, options)
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })
  const transports = []
  globalThis.fetch = (url, options) => {
    if (!url.endsWith('/compact-progress')) return Promise.resolve(response({ ok: true }))
    const pending = deferred()
    transports.push({ url, options, ...pending })
    options.signal.addEventListener('abort', () => pending.reject(options.signal.reason), { once: true })
    return pending.promise
  }
  t.mock.timers.enable({ apis: ['setTimeout'] })
  hook.rerender({ ...props, serverCompactingKind: 'compact' })
  assert.equal(transports.length, 1)
  t.mock.timers.tick(14999)
  assert.equal(transports[0].options.signal.aborted, false)
  t.mock.timers.tick(1)
  await flush()
  assert.equal(transports[0].options.signal.aborted, true)
  assert.equal(transports[0].options.signal.reason.name, 'TimeoutError')
  assert.equal(hook.result.current.compactProgressRecord.progress.recovery_id, 'saved')
  t.mock.timers.tick(60000)
  await flush()
  assert.equal(transports.length, 1, 'there is no progress polling or timeout retry')
  assert.equal(posts.length, 0, 'timeout cannot automatically continue compaction')
})


test('a server compaction edge GET cannot supersede the authoritative paused POST result', async t => {
  const { hook, props, reads, posts } = setup(t)
  reads[0].resolve(response({ progress: { state: 'running' } }))
  await flush()
  const action = hook.result.current.runCompactCommand('', null)
  hook.rerender({ ...props, serverCompactingKind: 'compact' })
  assert.equal(reads.length, 2)
  posts[0].resolve(response({ progress: { state: 'paused', recovery_id: 'continue-me' } }))
  await action
  assert.equal(hook.result.current.compactingChat, false)
  assert.equal(hook.result.current.compactProgressRecord.progress.state, 'paused')
  assert.equal(reads[1].options.signal.aborted, true)
  assert.equal(reads.length, 3, 'final readback stays deferred')
  reads[1].resolve(response({ progress: { state: 'running' } }))
  await flush()
  assert.equal(hook.result.current.compactProgressRecord.progress.recovery_id, 'continue-me')
  assert.equal(posts.length, 1)
})

test('a POST from a departed chat activation cannot overwrite a later activation of the same chat', async t => {
  const { hook, props, reads, posts, drafts, failures, messages } = setup(t)
  const action = hook.result.current.runCompactCommand()
  hook.rerender({ ...props, chatId: 'b' })
  hook.rerender(props)
  reads[2].resolve(response({ progress: { state: 'paused', recovery_id: 'current' } }))
  await flush()
  posts[0].resolve(response({ ok: true, progress: { state: 'completed' } }))
  await action
  assert.equal(hook.result.current.compactProgressRecord.progress.recovery_id, 'current')
  assert.equal(reads.length, 3)
  assert.deepEqual(drafts, [''])
  assert.deepEqual(failures, [null])
  assert.deepEqual(messages, [])
})

test('the progress deadline also cancels a shared-browser read without a transport timeout', async t => {
  const { hook, props, reads, posts } = setup(t)
  reads[0].resolve(response({ progress: { state: 'paused', recovery_id: 'saved' } }))
  await flush()
  const client = await import('../../../../api/client.js?compact-shared-deadline')
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })
  const transports = []
  globalThis.fetch = (url, options) => {
    if (url.endsWith('/browser-access/session/redeem')) return Promise.resolve(response({
      access_token: 'fixture-guest', token_type: 'bearer', expires_in: 900,
      grant: { id: 'fixture-grant' },
    }))
    assert.ok(url.endsWith('/compact-progress'))
    const pending = deferred()
    transports.push({ options, ...pending })
    options.signal.addEventListener('abort', () => pending.reject(options.signal.reason), { once: true })
    return pending.promise
  }
  client.beginSharedBrowserAuth()
  await client.redeemSharedBrowserInvite('fixture-invite')
  api.chats.compactProgress = (chatId, options) => client.apiFetch(`/chats/${chatId}/compact-progress`, options)
  t.mock.timers.enable({ apis: ['setTimeout'] })
  hook.rerender({ ...props, serverCompactingKind: 'compact' })
  await flush()
  assert.equal(transports.length, 1)
  t.mock.timers.tick(15000)
  await flush()
  assert.equal(transports[0].options.signal.aborted, true)
  assert.equal(transports[0].options.signal.reason.name, 'TimeoutError')
  assert.equal(hook.result.current.compactProgressRecord.progress.recovery_id, 'saved')
  assert.equal(posts.length, 0)
})
