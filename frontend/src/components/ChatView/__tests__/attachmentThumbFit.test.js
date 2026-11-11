import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import { attachmentThumbFit, markAttachmentThumbFit } from '../attachmentThumbFit.js'

// Render the composer and transcript tiles on the existing hook harness, so a
// test can fire the thumbnail's real load handler without a DOM. Children stay
// React elements; only these modules run their hooks.
const hooksPath = '/src/components/ChatView/hooks/__tests__/react-hook-shim.mjs'
const vite = await createSsrRenderServer({
  plugins: [{
    name: 'attachment-thumb-hooks', enforce: 'pre',
    transform(source, id) {
      if (/\/(FileChips|ImagePreviewButton|Attachments|useHistoryDismiss)\.jsx$/.test(id)) {
        return source.replaceAll("from 'react'", "from 'attachment-thumb-hooks'")
      }
    },
    resolveId(id) {
      if (id === 'attachment-thumb-hooks') return '\0attachment-thumb-hooks'
    },
    load(id) {
      if (id === '\0attachment-thumb-hooks') {
        return `export * from '${hooksPath}';
          export const useContext = () => null;
          export const createContext = () => ({ Provider: () => null });`
      }
    },
  }],
})
const { renderHook } = await vite.ssrLoadModule(hooksPath)
const { default: FileChips } = await vite.ssrLoadModule('/src/components/ChatView/FileChips.jsx')
const { default: Attachments } = await vite.ssrLoadModule('/src/components/ChatView/Attachments.jsx')
const { default: ImagePreviewButton } = await vite.ssrLoadModule(
  '/src/components/ChatView/ImagePreviewButton.jsx',
)
after(() => vite.close())

function elements(node) {
  if (!node || typeof node !== 'object') return []
  if (Array.isArray(node)) return node.flatMap(elements)
  return [node, ...elements(node.props?.children)]
}
function find(tree, predicate, what) {
  const element = elements(tree).find(predicate)
  assert.ok(element, `expected ${what}`)
  return element
}
const tick = () => new Promise(resolve => setImmediate(resolve))

/** The image element a load event reports, with its decoded size. */
function loadedImage(naturalWidth, naturalHeight) {
  return { naturalWidth, naturalHeight, dataset: {} }
}

test('extreme-aspect attachment thumbnails fit whole instead of a blank centre crop', () => {
  // A wide header strip's centre is often empty background; fit it whole.
  assert.equal(attachmentThumbFit(1080, 298), 'contain')
  assert.equal(attachmentThumbFit(298, 1080), 'contain')
  assert.equal(attachmentThumbFit(251, 100), 'contain')
  // Photos and ordinary phone screenshots keep the uniform square crop.
  assert.equal(attachmentThumbFit(250, 100), 'cover')
  assert.equal(attachmentThumbFit(1080, 1080), 'cover')
  assert.equal(attachmentThumbFit(4032, 3024), 'cover')
  assert.equal(attachmentThumbFit(1080, 2340), 'cover')
})

test('a thumbnail load records the measured fit for the stylesheet to read', () => {
  const banner = loadedImage(1080, 298)
  markAttachmentThumbFit({ currentTarget: banner })
  assert.equal(banner.dataset.fit, 'contain')

  const photo = loadedImage(4032, 3024)
  markAttachmentThumbFit({ currentTarget: photo })
  assert.equal(photo.dataset.fit, 'cover')
})

test('an image without a usable decoded size keeps the square crop', () => {
  for (const [width, height] of [[0, 0], [1080, 0], [Number.NaN, 298], [undefined, undefined]]) {
    const img = loadedImage(width, height)
    markAttachmentThumbFit({ currentTarget: img })
    assert.equal(img.dataset.fit, 'cover', `${width}x${height}`)
  }
})

test('a thumbnail that loads a different source re-measures instead of keeping the old fit', () => {
  // React reuses the <img> when its src changes; the next load must overwrite
  // the previous image's fit in both directions.
  const img = loadedImage(1080, 298)
  markAttachmentThumbFit({ currentTarget: img })
  assert.equal(img.dataset.fit, 'contain')

  img.naturalWidth = 1080
  img.naturalHeight = 1080
  markAttachmentThumbFit({ currentTarget: img })
  assert.equal(img.dataset.fit, 'cover')

  img.naturalHeight = 4000
  markAttachmentThumbFit({ currentTarget: img })
  assert.equal(img.dataset.fit, 'contain')
})

test('a pending composer image fits a wide banner whole once it loads', () => {
  const chips = renderHook(FileChips, {
    files: [{ id: 'banner', name: 'banner.png', mime_type: 'image/png', objectUrl: 'blob:banner' }],
    onRemove: () => {},
    chatId: 'thumb-chat',
  })
  try {
    const thumb = find(
      chips.result.current,
      node => node.type === 'img' && node.props.className === 'chat__attach-card-thumb',
      'composer thumbnail',
    )
    const img = loadedImage(1080, 298)
    thumb.props.onLoad({ currentTarget: img })
    assert.equal(img.dataset.fit, 'contain')
  } finally { chips.unmount() }
})

test('a sent image fits a wide banner whole once it loads', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async url => {
    assert.match(String(url), /\/chats\/thumb-chat\/media-token$/)
    return { ok: true, status: 200, json: async () => ({ token: 'media-token' }) }
  }
  let previewProps
  const message = renderHook(Attachments, {
    attachments: [{ name: 'banner.png', mime_type: 'image/png' }],
    chatId: 'thumb-chat',
  })
  try {
    await tick()
    const tile = find(
      message.result.current,
      node => typeof node.type === 'function' && node.props?.alt === 'banner.png',
      'sent image tile',
    )
    // The tile is a plain frame around the shared preview button.
    const preview = find(
      tile.type(tile.props),
      node => node.type === ImagePreviewButton,
      'sent image preview button',
    )
    previewProps = preview.props
  } finally {
    message.unmount()
    globalThis.fetch = originalFetch
  }

  const button = renderHook(ImagePreviewButton, previewProps)
  try {
    const thumb = find(
      button.result.current,
      node => node.type === 'img' && node.props.className === 'chat__attach-thumb',
      'sent thumbnail',
    )
    const img = loadedImage(298, 1080)
    thumb.props.onLoad({ currentTarget: img })
    assert.equal(img.dataset.fit, 'contain')
  } finally { button.unmount() }
})
