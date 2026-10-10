import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import * as hooks from '../hooks/__tests__/react-hook-shim.mjs'

// Execute the owning component's hooks and event handlers without a browser.
// JSX remains real React elements; only hooks and external token/history I/O
// are replaced by the existing synchronous hook harness and controlled stubs.
globalThis.__imageFailureHooks = hooks
globalThis.location = { origin: 'http://localhost' }
globalThis.window = { location: globalThis.location, innerHeight: 800 }
const vite = await createServer({
  appType: 'custom', logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui', 'dompurify'] },
  plugins: [{
    name: 'image-failure-boundaries', enforce: 'pre',
    resolveId(id) {
      if (id === 'virtual:image-hooks') return '\0image-hooks'
      if (id.endsWith('/api/mediaToken.js')) return '\0image-token'
      if (id.endsWith('/hooks/useHistoryDismiss.jsx')) return '\0image-history'
      if (id === 'dompurify') return '\0image-purify'
      return null
    },
    transform(code, id) {
      if (!id.endsWith('/markdown/InlineContent.jsx') && !id.endsWith('/ChatView/Attachments.jsx')) return null
      return code.replace("from 'react'", "from 'virtual:image-hooks'")
    },
    load(id) {
      if (id === '\0image-hooks') return 'export const { useState, useEffect, useRef } = globalThis.__imageFailureHooks'
      if (id === '\0image-token') return 'export const mediaTokenParam = (...args) => globalThis.__imageFailureToken(...args)'
      if (id === '\0image-history') return 'export const useHistoryDismiss = () => ({ open() {}, close() {} })'
      if (id === '\0image-purify') return 'export default { sanitize: value => String(value) }'
      return null
    },
  }],
})
const { ExpandableImage } = await vite.ssrLoadModule('/src/components/ChatView/markdown/InlineContent.jsx')
const { default: Attachments } = await vite.ssrLoadModule('/src/components/ChatView/Attachments.jsx')
after(async () => {
  await vite.close()
  delete globalThis.__imageFailureHooks
  delete globalThis.__imageFailureToken
})

function findElement(node, predicate) {
  if (Array.isArray(node)) return node.map(child => findElement(child, predicate)).find(Boolean) || null
  if (!node || typeof node !== 'object') return null
  if (predicate(node)) return node
  return findElement(node.props?.children, predicate)
}
const frame = tree => findElement(tree, node => node.props?.className === 'md-image-frame')
const image = tree => findElement(tree, node => node.type === 'img')
const href = '/api/chats/image-chat/generated-files/image.png'

for (const knownDimensions of [false, true]) {
  for (const failure of ['image', 'empty-token', 'rejected-token']) {
    test(`${failure} retains ${knownDimensions ? 'known' : 'default'} reserved geometry`, async () => {
      let resolveToken, rejectToken
      globalThis.__imageFailureToken = () => new Promise((resolve, reject) => {
        resolveToken = resolve
        rejectToken = reject
      })
      let opened = 0
      const mounted = hooks.renderHook(() => ExpandableImage({
        href, alt: 'Generated picture', onOpen: () => { opened += 1 },
        mediaDimensions: knownDimensions ? { [href]: { width: 1536, height: 1024 } } : {},
      }))
      try {
        const before = frame(mounted.result.current)
        assert.ok(before)
        assert.equal(before.props['aria-label'], 'Open Generated picture preview')
        const geometry = before.props.style
        if (knownDimensions) assert.equal(geometry['--md-image-ratio'], '1536 / 1024')
        else assert.equal(geometry, undefined)
        if (failure === 'rejected-token') rejectToken(new Error('network unavailable'))
        else resolveToken(failure === 'empty-token' ? null : '?token=MEDIA_ONLY')
        await Promise.resolve()
        if (failure === 'image') {
          const pending = image(mounted.result.current)
          assert.ok(pending)
          pending.props.onError()
        }
        const afterFailure = frame(mounted.result.current)
        assert.ok(afterFailure, 'asynchronous failure must not remove the reserved frame')
        assert.deepEqual(afterFailure.props.style, geometry)
        assert.equal(afterFailure.props.disabled, true)
        assert.equal(afterFailure.props['aria-busy'], false)
        assert.match(afterFailure.props['aria-label'], /unavailable/i)
        const feedback = findElement(afterFailure, node => node.props?.role === 'status')
        assert.equal(feedback.props.children, 'Image unavailable')
        assert.equal(image(mounted.result.current), null)
        afterFailure.props.onClick()
        assert.equal(opened, 0)
      } finally {
        mounted.unmount()
      }
    })
  }
}

test('an explicit server dimension verdict remains an immediate unavailable image', () => {
  let requests = 0
  globalThis.__imageFailureToken = () => { requests += 1; return Promise.resolve(null) }
  const mounted = hooks.renderHook(() => ExpandableImage({
    href, alt: 'Generated picture', mediaDimensions: { [href]: null },
  }))
  try {
    assert.equal(frame(mounted.result.current), null)
    assert.ok(findElement(mounted.result.current, node => node.props?.role === 'img'))
    assert.equal(requests, 0)
  } finally {
    mounted.unmount()
  }
})


test('authorized historical generated gallery images use the lazy default', async () => {
  globalThis.__imageFailureToken = () => Promise.resolve('?token=MEDIA_ONLY')
  const names = ['first.png', 'second.png', 'last.png']
  const mediaDimensions = Object.fromEntries(names.map(name => [
    `/api/chats/image-chat/generated-files/${name}`, { width: 120, height: 300 },
  ]))
  const gallery = hooks.renderHook(() => Attachments({
    attachments: names.map(name => ({
      kind: 'generated', name, mime_type: 'image/png', previewable: true,
    })), chatId: 'image-chat', mediaDimensions,
  }))
  try {
    for (const name of names) {
      const child = findElement(gallery.result.current,
        node => node.type === ExpandableImage && node.props.alt === name)
      assert.ok(child, 'owning gallery must retain every generated image')
      const mounted = hooks.renderHook(() => ExpandableImage(child.props))
      try {
        const geometry = frame(mounted.result.current).props.style
        assert.equal(geometry['--md-image-ratio'], '120 / 300')
        assert.equal(image(mounted.result.current), null, 'token pending')
        await Promise.resolve()
        const resolved = image(mounted.result.current)
        assert.ok(resolved, 'authorized original image must render')
        assert.equal(resolved.props.loading, 'lazy')
        assert.equal(resolved.props.decoding, 'async')
        assert.match(resolved.props.src, /generated-files\/.*\?token=MEDIA_ONLY&preview=true$/)
        assert.deepEqual(frame(mounted.result.current).props.style, geometry)
      } finally {
        mounted.unmount()
      }
    }
  } finally {
    gallery.unmount()
  }
})
