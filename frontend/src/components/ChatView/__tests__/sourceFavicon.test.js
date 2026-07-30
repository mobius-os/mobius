import test from 'node:test'
import assert from 'node:assert/strict'
import {
  loadSourceFavicon,
  safeSvgFavicon,
  sourceFaviconCandidateUrls,
  sourceFaviconProxyPath,
  sourceFaviconResolverPath,
  validatedFaviconBlob,
} from '../SourceFavicon.jsx'

const ico = Uint8Array.from([0x00, 0x00, 0x01, 0x00, 0x01, 0x00])
const response = (body, contentType = 'image/x-icon', status = 200) => new Response(body, {
  status,
  headers: { 'content-type': contentType },
})

test('favicon URLs become authenticated same-origin proxy reads', () => {
  assert.equal(
    sourceFaviconProxyPath('https://www.example.com/favicon.ico'),
    '/proxy?url=https%3A%2F%2Fwww.example.com%2Ffavicon.ico',
  )
  assert.equal(sourceFaviconProxyPath('http://user:pass@example.com/favicon.ico'), '')
  assert.equal(sourceFaviconProxyPath('data:image/png;base64,eA=='), '')
  assert.equal(
    sourceFaviconResolverPath('https://www.example.com/'),
    '/proxy/favicon?url=https%3A%2F%2Fwww.example.com%2F',
  )
  assert.equal(sourceFaviconResolverPath('http://user:pass@example.com/'), '')
})

test('favicon candidates cover common root icon conventions without third parties', () => {
  assert.deepEqual(
    sourceFaviconCandidateUrls('https://example.com/favicon.ico'),
    [
      'https://example.com/favicon.ico',
      'https://example.com/favicon.svg',
      'https://example.com/favicon.png',
      'https://example.com/apple-touch-icon.png',
    ],
  )
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

test('safe self-contained SVG favicons are accepted and active SVG is rejected', async () => {
  const safe = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1z"/></svg>'
  assert.equal(safeSvgFavicon(safe), true)
  const icon = await validatedFaviconBlob(response(safe, 'image/svg+xml'))
  assert.equal(icon.type, 'image/svg+xml')

  for (const unsafe of [
    '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://tracker.example/x"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><style>@import "https://x"</style></svg>',
  ]) {
    assert.equal(safeSvgFavicon(unsafe), false)
    await assert.rejects(
      validatedFaviconBlob(response(unsafe, 'image/svg+xml')),
      /safe self-contained image/,
    )
  }
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

test('a missing favicon.ico falls through to a safe favicon.svg', async (t) => {
  const originalFetch = globalThis.fetch
  const reads = []
  globalThis.fetch = async (url) => {
    reads.push(String(url))
    if (String(url).includes('favicon.ico')) {
      return response('missing', 'text/plain', 404)
    }
    return response(
      '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1z"/></svg>',
      'image/svg+xml',
    )
  }
  t.after(() => { globalThis.fetch = originalFetch })

  const icon = await loadSourceFavicon('https://svg.example/favicon.ico')
  assert.equal(icon.type, 'image/svg+xml')
  assert.equal(reads.length, 2)
  assert.match(reads[0], /favicon\.ico/)
  assert.match(reads[1], /favicon\.svg/)
})

test('the site resolver is preferred when available', async (t) => {
  const originalFetch = globalThis.fetch
  const reads = []
  globalThis.fetch = async (url) => {
    reads.push(String(url))
    if (String(url).includes('/proxy/favicon?')) return response(ico)
    return response('missing', 'text/plain', 404)
  }
  t.after(() => { globalThis.fetch = originalFetch })

  const icon = await loadSourceFavicon(
    'https://declared.example/favicon.ico',
    'https://declared.example/',
  )
  assert.equal(icon.type, 'image/x-icon')
  assert.equal(reads.length, 1)
  assert.match(reads[0], /\/proxy\/favicon\?url=/)
})

test('root conventions remain available while the resolver awaits restart', async (t) => {
  const originalFetch = globalThis.fetch
  const reads = []
  globalThis.fetch = async (url) => {
    reads.push(String(url))
    if (String(url).includes('/proxy/favicon?')) {
      return response('missing', 'application/json', 404)
    }
    if (String(url).includes('favicon.ico')) {
      return response('missing', 'text/plain', 404)
    }
    return response(ico)
  }
  t.after(() => { globalThis.fetch = originalFetch })

  const icon = await loadSourceFavicon(
    'https://restart.example/favicon.ico',
    'https://restart.example/',
  )
  assert.equal(icon.type, 'image/x-icon')
  assert.equal(reads.length, 3)
  assert.match(reads[0], /\/proxy\/favicon\?url=/)
  assert.match(reads[1], /favicon\.ico/)
  assert.match(reads[2], /favicon\.svg/)
})
