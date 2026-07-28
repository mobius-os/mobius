import test from 'node:test'
import assert from 'node:assert/strict'
import {
  loadSourceFavicon,
  sourceFaviconProxyPath,
  validatedFaviconBlob,
} from '../SourceFavicon.jsx'

const ico = Uint8Array.from([0x00, 0x00, 0x01, 0x00, 0x01, 0x00])
const response = (body, contentType = 'image/x-icon') => new Response(body, {
  headers: { 'content-type': contentType },
})

test('favicon URLs become authenticated same-origin proxy reads', () => {
  assert.equal(
    sourceFaviconProxyPath('https://www.example.com/favicon.ico'),
    '/proxy?url=https%3A%2F%2Fwww.example.com%2Ffavicon.ico',
  )
  assert.equal(sourceFaviconProxyPath('http://user:pass@example.com/favicon.ico'), '')
  assert.equal(sourceFaviconProxyPath('data:image/png;base64,eA=='), '')
})

test('favicon responses require a bounded, supported raster image', async () => {
  const icon = await validatedFaviconBlob(response(ico, 'application/octet-stream'))
  assert.equal(icon.type, 'image/x-icon')
  assert.equal(icon.size, ico.length)

  await assert.rejects(
    validatedFaviconBlob(response('<html>not an icon</html>', 'text/html')),
    /Unsupported favicon content type/,
  )
  await assert.rejects(
    validatedFaviconBlob(response('<html>not an icon</html>', 'image/x-icon')),
    /not a supported image/,
  )
  await assert.rejects(
    validatedFaviconBlob(response(new Uint8Array(256 * 1024 + 1))),
    /too large/,
  )
})

test('repeated citations share one in-flight proxy read', async (t) => {
  const originalFetch = globalThis.fetch
  let reads = 0
  let release
  const waiting = new Promise(resolve => { release = resolve })
  globalThis.fetch = async () => {
    reads += 1
    await waiting
    return response(ico)
  }
  t.after(() => { globalThis.fetch = originalFetch })

  const faviconUrl = 'https://dedupe.example/favicon.ico'
  const first = loadSourceFavicon(faviconUrl)
  const second = loadSourceFavicon(faviconUrl)
  assert.equal(first, second)
  assert.equal(reads, 1)
  release()
  await Promise.all([first, second])
})
