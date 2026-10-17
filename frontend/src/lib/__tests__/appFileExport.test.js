import test from 'node:test'
import assert from 'node:assert/strict'
import {
  appFileExportIsCurrent, appFileExportUrl, beginAppFileExport, finishAppFileExport,
  MAX_APP_EXPORT_BYTES, parseAppFileExport, readAppFileExport,
} from '../appFileExport.js'
import {
  attributedFrameVersion, confirmAppFileExport, retireStaleAppFileExport,
  serveAppFileExportRequest,
} from '../../components/AppCanvas/appFrameProtocol.js'

test('file export accepts only a bounded relative own-file path and safe name', () => {
  const message = { type: 'moebius:file-export', requestId: 'r1', path: 'library/files/abc.mp3', name: 'Artist / Track.mp3' }
  assert.deepEqual(parseAppFileExport(message), {
    requestId: 'r1', path: 'library/files/abc.mp3', name: 'Artist _ Track.mp3',
  })
  for (const path of ['../secret', '/absolute', 'a//b', 'a/./b', 'a/../b', 'a\\b', 'a\u0000b']) {
    assert.equal(parseAppFileExport({ ...message, path }), null, path)
  }
  assert.equal(appFileExportUrl(12, 'library/files/a b.mp3'), '/api/storage/apps/12/library/files/a%20b.mp3')
  assert.equal(parseAppFileExport({ ...message, name: 'song\u202Emp3.exe' }).name, 'song_mp3.exe')
  assert.equal(parseAppFileExport({ ...message, name: 'song\u2067safe\u2069.exe' }).name, 'song_safe_.exe')
})

test('file export reads only the host-selected app with the scoped bearer', async () => {
  const calls = []
  const blob = await readAppFileExport({
    appId: 12, path: 'library/files/abc.mp3', token: 'scoped',
    fetchFile: async (...args) => {
      calls.push(args)
      return new Response(new Uint8Array([1, 2, 3]), { headers: { 'content-type': 'audio/mpeg' } })
    },
  })
  assert.equal(blob.type, 'audio/mpeg')
  assert.deepEqual([...new Uint8Array(await blob.arrayBuffer())], [1, 2, 3])
  assert.equal(calls[0][0], '/api/storage/apps/12/library/files/abc.mp3')
  assert.equal(calls[0][1].headers.Authorization, 'Bearer scoped')
})

test('file export rejects a declared oversized response before reading it', async () => {
  await assert.rejects(readAppFileExport({
    appId: 12, path: 'big.mp3', token: 'scoped',
    fetchFile: async () => new Response('x', { headers: { 'content-length': String(MAX_APP_EXPORT_BYTES + 1) } }),
  }), /too large/)
})

test('file export aborts an active stream instead of retaining its chunks', async () => {
  const controller = new AbortController()
  let cancelled = false
  let reading
  const readingStarted = new Promise(resolve => { reading = resolve })
  const stream = new ReadableStream({
    pull(source) {
      source.enqueue(new Uint8Array([1, 2, 3]))
      reading()
      return new Promise(() => {})
    },
    cancel() { cancelled = true },
  })
  const pending = readAppFileExport({
    appId: 12, path: 'large.mp3', token: 'scoped', signal: controller.signal,
    fetchFile: async (_url, options) => {
      assert.equal(options.signal, controller.signal)
      return new Response(stream)
    },
  })
  await readingStarted
  controller.abort()
  await assert.rejects(pending, { name: 'AbortError' })
  assert.equal(cancelled, true)
})

test('file export does not begin fetching after cancellation', async () => {
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(readAppFileExport({
    appId: 12, path: 'song.mp3', token: 'scoped', signal: controller.signal,
    fetchFile: () => { throw new Error('fetch must not run') },
  }), { name: 'AbortError' })
})

test('broker admits only the live visible frame before opening a confirmation', () => {
  const replies = []
  const live = { postMessage: message => replies.push(['live', message]) }
  const buffered = { postMessage: message => replies.push(['buffered', message]) }
  const frames = new Map([[1, { contentWindow: live }], [2, { contentWindow: buffered }]])
  const message = { type: 'moebius:file-export', requestId: 'r1', path: 'library/song.mp3' }
  const begin = (source, visible, pending = null) => beginAppFileExport({
    message, source, visible, pending, appId: 12,
    sourceVersion: attributedFrameVersion(frames, source), liveVersion: 1,
  })
  assert.equal(begin(buffered, true), null)
  assert.equal(begin(live, false), null)
  assert.equal(begin({ postMessage: message => replies.push(['stale', message]) }, true), null)
  assert.deepEqual(replies.map(([, result]) => result.cancelled), [true, true, true])
  const request = begin(live, true)
  assert.equal(request.source, live)
  assert.equal(request.frameVersion, 1)
  assert.equal(replies.length, 3) // the accepted request only opens confirmation
  assert.equal(begin(buffered, true, request), null)
  assert.equal(begin(live, true, request), null)
  assert.equal(replies[3][1].cancelled, true)
  assert.match(replies[4][1].error, /current file export/)
})

test('broker retires a pending export when its frame is hidden or promoted away', () => {
  const replies = []
  const source = { postMessage: message => replies.push(message) }
  const request = beginAppFileExport({
    message: { type: 'moebius:file-export', requestId: 'r2', path: 'library/song.mp3' },
    source, sourceVersion: 1, liveVersion: 1, visible: true, pending: null, appId: 12,
  })
  assert.equal(appFileExportIsCurrent({ request, appId: 12, visible: true,
    liveVersion: 1, sourceVersion: 1 }), true)
  assert.equal(appFileExportIsCurrent({ request, appId: 12, visible: false,
    liveVersion: 1, sourceVersion: 1 }), false)
  assert.equal(appFileExportIsCurrent({ request, appId: 12, visible: true,
    liveVersion: 2, sourceVersion: 1 }), false)
  assert.equal(appFileExportIsCurrent({ request, appId: 12, visible: true,
    liveVersion: 1, sourceVersion: null }), false)
  const controller = new AbortController()
  const busy = { ...request, controller, busy: true }
  const pendingRef = { current: busy }
  const pendingStates = []
  assert.equal(finishAppFileExport({ request: busy, pendingRef,
    setPending: value => pendingStates.push(value), result: { cancelled: true } }), true)
  assert.equal(controller.signal.aborted, true)
  assert.equal(pendingRef.current, null)
  assert.deepEqual(pendingStates, [null])
  assert.deepEqual(replies, [{ type: 'moebius:file-export-result', requestId: 'r2', cancelled: true }])
  assert.equal(finishAppFileExport({ request: busy, pendingRef,
    setPending: () => { throw new Error('duplicate settlement') }, result: { ok: true } }), false)
})

test('AppCanvas protocol routes only the live frame, reads only after confirmation, and cancels on hide', async () => {
  const replies = []
  const live = { postMessage: message => replies.push(['live', message]) }
  const buffered = { postMessage: message => replies.push(['buffered', message]) }
  const frames = new Map([[1, { contentWindow: live }], [2, { contentWindow: buffered }]])
  const pendingRef = { current: null }
  const prompts = []
  const setPending = value => prompts.push(value)
  const message = { type: 'moebius:file-export', requestId: 'r3', path: 'library/song.mp3' }
  const serve = (source, visible = true) => serveAppFileExportRequest({
    message, source, frames, liveVersion: 1, visible,
    pendingRef, setPending, appId: 12,
  })
  assert.equal(serve(buffered), true)
  assert.equal(pendingRef.current, null)
  assert.equal(prompts.length, 0)
  assert.equal(replies[0][1].cancelled, true)
  assert.equal(serve({ postMessage: () => { throw new Error('unknown frame replied') } }), true)
  assert.equal(prompts.length, 0)
  assert.equal(serve(live), true)
  assert.equal(prompts.length, 1)
  let reads = 0
  const saves = []
  const confirm = readFile => confirmAppFileExport({ pendingRef, setPending, appId: 12,
    getVisible: () => true, getLiveVersion: () => 1,
    getSourceVersion: source => attributedFrameVersion(frames, source),
    getToken: () => 'scoped', readFile, saveFile: (...args) => saves.push(args) })
  assert.equal(reads, 0) // request handling only opened the confirmation
  await confirm(async ({ appId, path, token }) => {
    reads += 1
    assert.deepEqual([appId, path, token], [12, 'library/song.mp3', 'scoped'])
    return new Blob(['audio'])
  })
  assert.equal(reads, 1)
  assert.equal(saves.length, 1)
  assert.equal(replies.at(-1)[1].ok, true)
  assert.equal(pendingRef.current, null)

  assert.equal(serve(live), true)
  let resolveRead
  const reading = confirm(({ signal }) => {
    assert.equal(signal.aborted, false)
    return new Promise(resolve => { resolveRead = resolve })
  })
  assert.equal(prompts.at(-1).busy, true)
  const activeSignal = prompts.at(-1).controller.signal
  assert.equal(retireStaleAppFileExport({ pendingRef, setPending, appId: 12,
    visible: false, liveVersion: 1, frames }), true)
  assert.equal(activeSignal.aborted, true)
  resolveRead(new Blob(['late']))
  await reading
  assert.equal(saves.length, 1) // a retired frame cannot download late data
  assert.equal(replies.at(-1)[1].cancelled, true)
  assert.equal(pendingRef.current, null)
})

test('busy export can be cancelled without waiting for a stalled read', async () => {
  const replies = []
  const source = { postMessage: message => replies.push(message) }
  const frames = new Map([[1, { contentWindow: source }]])
  const pendingRef = { current: null }
  const setPending = () => {}
  const message = { type: 'moebius:file-export', requestId: 'slow', path: 'song.mp3' }
  serveAppFileExportRequest({ message, source, frames, liveVersion: 1,
    visible: true, pendingRef, setPending, appId: 12 })
  let resolveRead
  const saves = []
  const confirmation = confirmAppFileExport({ pendingRef, setPending, appId: 12,
    getVisible: () => true, getLiveVersion: () => 1,
    getSourceVersion: value => attributedFrameVersion(frames, value),
    getToken: () => 'scoped',
    readFile: () => new Promise(resolve => { resolveRead = resolve }),
    saveFile: (...args) => saves.push(args) })
  const busy = pendingRef.current
  assert.equal(busy.busy, true)
  assert.equal(finishAppFileExport({ request: busy, pendingRef, setPending,
    result: { cancelled: true } }), true)
  assert.equal(busy.controller.signal.aborted, true)
  assert.equal(pendingRef.current, null)
  assert.deepEqual(replies, [{ type: 'moebius:file-export-result',
    requestId: 'slow', cancelled: true }])
  resolveRead(new Blob(['late']))
  await confirmation
  assert.deepEqual(saves, [])
})
