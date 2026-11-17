/* Reply source reads are lazy, stable, partial-failure-safe and source-scoped. */
import test from 'node:test'
import assert from 'node:assert/strict'
import useMessageSources from '../useMessageSources.js'
import { renderHook } from './react-hook-shim.mjs'
const flush = async () => { for (let i = 0; i < 18; i++) await Promise.resolve() }
const response = sources => ({ ok: true, json: async () => ({ sources }) })
function reader() {
 const calls = []
 const request = (url, options) => new Promise((resolve, reject) => calls.push({ url, options, resolve, reject }))
 return { calls, request }
}
const ref = message_index => ({ message_index, count: 1 })
const source = url => ({ url })

test('closed References are network-free; equivalent reply renders do not cancel reads', async () => {
 const { calls, request } = reader()
 const props = { chatId: 'chat', groups: [], refs: [ref(1), ref(3)], open: false, request }
 const hook = renderHook(useMessageSources, props)
 assert.equal(calls.length, 0)
 hook.rerender({ ...props, open: true })
 for (let i = 0; i < 30; i++) hook.rerender({ ...props, open: true, refs: [ref(1), ref(3)], groups: [] })
 assert.equal(calls.length, 2)
 assert.equal(calls[0].options.signal.aborted, false)
 calls[1].resolve(response([source('https://b.example')]))
 calls[0].resolve(response([source('https://a.example')]))
 await flush()
 assert.deepEqual(hook.result.current.sources.map(s => s.url), ['https://a.example', 'https://b.example'])
 assert.equal(hook.result.current.complete, true)
 hook.unmount()
})

test('failed lazy indices retain successful references; retry only reads missing pages', async () => {
 const { calls, request } = reader()
 const hook = renderHook(useMessageSources, { chatId: 'chat', groups: [], refs: [ref(1), ref(3)], open: true, request })
 calls[0].resolve(response([source('https://a.example')]))
 calls[1].reject(new Error('temporary failure'))
 await flush()
 assert.equal(hook.result.current.failed, true)
 assert.equal(hook.result.current.sources.length, 1)
 assert.equal(hook.result.current.count, null, 'do not add overlapping unknown message counts')
 hook.result.current.retry()
 assert.equal(calls.length, 3)
 assert.match(calls[2].url, /message_index=3/)
 calls[2].resolve(response([source('https://a.example'), source('https://b.example')]))
 await flush()
 assert.equal(hook.result.current.sources.length, 2)
 assert.equal(hook.result.current.count, 2)
 assert.equal(hook.result.current.failed, false)
 hook.unmount()
})

test('per-message safety limits do not discard references from other reply segments', async () => {
 const { calls, request } = reader()
 const hook = renderHook(useMessageSources, { chatId: 'chat', groups: [], refs: [ref(1), ref(3)], open: true, request })
 calls[0].resolve(response(Array.from({ length: 24 }, (_, i) => source(`https://a.example/${i}`))))
 calls[1].resolve(response(Array.from({ length: 24 }, (_, i) => source(`https://b.example/${i}`))))
 await flush()
 assert.equal(hook.result.current.sources.length, 48)
 assert.equal(hook.result.current.count, 48)
 hook.unmount()
})

test('chat changes and canceled disclosure reads cannot publish stale source pages', async () => {
 const { calls, request } = reader()
 const props = { chatId: 'first', groups: [], refs: [ref(1)], open: true, request }
 const hook = renderHook(useMessageSources, props)
 hook.rerender({ ...props, chatId: 'second' })
 assert.equal(calls[0].options.signal.aborted, true)
 calls[0].resolve(response([source('https://old.example')]))
 calls[1].resolve(response([source('https://new.example')]))
 await flush()
 assert.deepEqual(hook.result.current.sources.map(s => s.url), ['https://new.example'])
 hook.rerender({ ...props, chatId: 'third' })
 hook.rerender({ ...props, chatId: 'third', open: false })
 assert.equal(calls[2].options.signal.aborted, true)
 calls[2].resolve(response([source('https://closed.example')]))
 await flush()
 assert.equal(hook.result.current.sources.length, 0)
 hook.unmount()
})


test('new inline sources do not cancel or discard the same reply’s lazy source pages', async () => {
 const { calls, request } = reader()
 const props = { chatId: 'chat', groups: [], refs: [ref(1)], open: true, request }
 const hook = renderHook(useMessageSources, props)
 hook.rerender({ ...props, groups: [[{ type: 'tool', sources: [source('https://inline.example')] }]] })
 assert.equal(calls.length, 1)
 assert.equal(calls[0].options.signal.aborted, false)
 calls[0].resolve(response([source('https://saved.example')]))
 await flush()
 assert.deepEqual(hook.result.current.sources.map(s => s.url), ['https://inline.example', 'https://saved.example'])
 assert.equal(hook.result.current.complete, true)
 hook.unmount()
})

test('an unchanged answer reuses its parsed sources across re-renders', () => {
 const blocks = [{ type: 'tool', sources: [source('https://a.example'), source('https://b.example')] }, { type: 'text', content: 'Answer' }]
 const props = { chatId: 'chat', groups: [blocks], refs: [], open: false }
 const hook = renderHook(useMessageSources, props)
 const first = hook.result.current.sources
 assert.deepEqual(first.map(s => s.url), ['https://a.example', 'https://b.example'])
 // A streamed frame rebuilds the groups array around the same blocks.
 hook.rerender({ ...props, groups: [blocks] })
 assert.equal(hook.result.current.sources, first)
 const live = [...blocks, { type: 'tool', sources: [source('https://c.example')] }]
 hook.rerender({ ...props, groups: [live] })
 assert.notEqual(hook.result.current.sources, first, 'changed blocks are read again')
 assert.equal(hook.result.current.sources.length, 3)
 hook.unmount()
})
