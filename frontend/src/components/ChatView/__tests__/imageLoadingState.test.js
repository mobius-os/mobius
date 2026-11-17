/* Pending chat images must explain their reserved space without shifting it. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

const location = { origin: 'http://localhost', href: 'http://localhost/shell/' }
globalThis.window = { location, innerWidth: 900, innerHeight: 800 }
globalThis.location = location
const vite = await createSsrRenderServer({
  noExternal: ['dompurify'],
  plugins: [{
    name: 'dompurify-ssr-stub', enforce: 'pre',
    resolveId: id => id === 'dompurify' ? '\0dompurify-stub' : null,
    load: id => id === '\0dompurify-stub'
      ? 'export default { sanitize: value => String(value) }' : null,
  }],
})
after(() => vite.close())
const { ExpandableImage } = await vite.ssrLoadModule('/src/components/ChatView/markdown/InlineContent.jsx')
const href = '/api/chats/image-fixture/media/preview.png'

function render(extra = {}) {
  return renderToStaticMarkup(createElement(ExpandableImage, {
    href, alt: 'A chart', imageIndex: 0, ...extra,
  }))
}

test('a valid unresolved image shows loading inside its reserved frame on first paint', () => {
  const html = render()
  assert.match(html, /class="md-image-frame"/)
  assert.match(html, /aria-busy="true"/)
  assert.match(html, /Loading image/)
  assert.doesNotMatch(html, /<img/, 'token resolution has not mounted the image yet')
})

test('the pending treatment preserves server-provided frame dimensions', () => {
  const html = render({ mediaDimensions: { [href]: { width: 800, height: 400 } } })
  assert.match(html, /--md-image-ratio:800 \/ 400/)
  assert.match(html, /--md-image-fit-width:/)
  assert.match(html, /Loading image/)
})

test('invalid and explicitly unreadable paths retain their existing semantics', () => {
  assert.equal(render({ href: 'javascript:alert(1)' }), '')
  const html = render({ mediaDimensions: { [href]: null } })
  assert.match(html, /Image unavailable/)
  assert.doesNotMatch(html, /Loading image/)
})
