import test from 'node:test'
import assert from 'node:assert/strict'

import {
  assembleProjectHtmlPreview,
  projectPreviewSandbox,
  safeProjectHtmlDocument,
} from '../projectPreview.js'

test('project HTML preview injects a deny-by-default CSP into the document head', () => {
  const result = safeProjectHtmlDocument('<html><head><title>Site</title></head><body /></html>')
  assert.match(result, /^<meta http-equiv="Content-Security-Policy"/)
  assert.match(result, /default-src 'none'/)
  assert.match(result, /form-action 'none'/)
  assert.match(result, /base-uri 'none'/)
  assert.match(result, /script-src 'unsafe-inline'/)
  assert.match(result, /data-mobius-project-preview-runtime/)
  assert.match(result, /dataScope: 'personal'/)
  assert.match(result, /mobius:project-preview-storage-connected/)
  assert.doesNotMatch(result, /setInterval\(post, 100\)/)
  assert.match(result, /Personal preview data did not connect/)
})

test('project preview policy precedes resources placed before a malformed head', () => {
  const remoteImage = '<img src="https://tracker.invalid/pixel"><head><title>Late head</title>'
  const result = safeProjectHtmlDocument(remoteImage)

  assert.equal(result.indexOf('Content-Security-Policy') < result.indexOf(remoteImage), true)
  assert.match(result, /^<meta http-equiv="Content-Security-Policy"/)
})

test('project HTML preview runs scripts without granting origin or navigation access', () => {
  assert.equal(projectPreviewSandbox(), 'allow-scripts')
})

test('shared app preview exposes the same storage surface with a truthful scope', () => {
  const result = safeProjectHtmlDocument('<main>Shared</main>', 'shared')
  assert.match(result, /dataScope: 'shared'/)
  assert.doesNotMatch(result, /dataScope: 'personal'/)
  assert.match(result, /Shared app data did not connect/)
  assert.doesNotMatch(result, /Personal preview data did not connect/)
})

test('project HTML preview inlines local CSS and JavaScript into its isolated document', async () => {
  const files = new Map([
    ['site/style.css', 'body { color: rebeccapurple; }'],
    ['site/app.js', 'document.body.dataset.ready = "yes"'],
  ])
  const result = await assembleProjectHtmlPreview(
    '<link rel="stylesheet" href="./style.css"><script src="./app.js"></script>',
    'site/index.html',
    async path => {
      if (!files.has(path)) throw new Error('missing')
      return files.get(path)
    },
  )
  assert.match(result, /data-project-file="site\/style.css"/)
  assert.match(result, /color: rebeccapurple/)
  assert.match(result, /data-project-file="site\/app.js"/)
  assert.match(result, /dataset.ready/)
})

test('project HTML preview keeps JavaScript replacement tokens literal', async () => {
  const result = await assembleProjectHtmlPreview(
    '<script src="app.js"></script>',
    'index.html',
    async path => {
      assert.equal(path, 'app.js')
      return 'const replacementTokens = "$& $` $\'"; window.previewReady = true'
    },
  )

  assert.match(result, /replacementTokens = "\$& \$` \$'"/)
  assert.equal((result.match(/<script src="app\.js"><\/script>/g) || []).length, 0)
  assert.match(result, /window\.previewReady = true/)
})

test('project HTML preview leaves remote dependencies blocked by CSP', async () => {
  const result = await assembleProjectHtmlPreview(
    '<script src="https://tracker.invalid/x.js"></script>',
    'index.html',
    async () => { throw new Error('must not fetch') },
  )
  assert.match(result, /https:\/\/tracker.invalid\/x.js/)
  assert.match(result, /default-src 'none'/)
})

test('project HTML preview inlines local images and CSS url() as data URIs', async () => {
  const assets = new Map([
    ['site/logo.png', 'data:image/png;base64,AAAA'],
    ['site/bg.jpg', 'data:image/jpeg;base64,BBBB'],
  ])
  const result = await assembleProjectHtmlPreview(
    '<style>.hero{background:url("./bg.jpg")}</style><img src="./logo.png">',
    'site/index.html',
    async () => { throw new Error('no text deps') },
    async path => {
      if (!assets.has(path)) throw new Error('missing')
      return assets.get(path)
    },
  )
  assert.match(result, /<img src="data:image\/png;base64,AAAA">/)
  assert.match(result, /url\(data:image\/jpeg;base64,BBBB\)/)
})

test('project HTML preview leaves remote images untouched (CSP-blocked)', async () => {
  const result = await assembleProjectHtmlPreview(
    '<img src="https://tracker.invalid/pixel.gif">',
    'index.html',
    async () => { throw new Error('no text deps') },
    async () => { throw new Error('must not fetch remote') },
  )
  assert.match(result, /https:\/\/tracker.invalid\/pixel.gif/)
})

test('project HTML preview without a data loader keeps local images as-is', async () => {
  const result = await assembleProjectHtmlPreview(
    '<img src="./logo.png">',
    'site/index.html',
    async () => { throw new Error('no text deps') },
  )
  assert.match(result, /<img src="\.\/logo.png">/)
})

test('inherited theme updates as text from the parent without widening preview permissions', async () => {
  const { runInNewContext } = await import('node:vm')
  const html = safeProjectHtmlDocument('<meta name="mobius-theme" content="inherit">')
  const script = html.match(/<script data-mobius-project-preview-runtime>([\s\S]*?)<\/script>/)[1]
  const parent = {}
  const listeners = new Map()
  const elements = new Map()
  const document = {
    getElementById: id => elements.get(id),
    createElement: () => ({}),
    head: { prepend: style => elements.set(style.id, style) },
    documentElement: { dataset: {}, style: {} },
  }
  const window = {}
  runInNewContext(script, {
    parent, document, window, Map, Promise, setTimeout, clearTimeout,
    addEventListener: (type, fn) => listeners.set(type, fn),
    dispatchEvent() {}, CustomEvent: class {},
  })
  const css = ':root { --bg: #eee; } /* </style><script>untrusted</script> */'
  const message = { type: 'mobius:project-theme', theme: { css, mode: 'light' } }
  listeners.get('message')({ source: {}, data: message })
  assert.equal(elements.size, 0)
  listeners.get('message')({ source: parent, data: message })
  assert.equal(elements.size, 1)
  assert.ok(elements.get('mobius-inherited-project-theme').textContent.startsWith(css))
  assert.equal(document.documentElement.dataset.theme, 'light')
  listeners.get('message')({ source: parent, data: { ...message, theme: { css: ':root { --bg: #111; }', mode: 'dark' } } })
  assert.equal(elements.size, 1)
  assert.equal(document.documentElement.style.colorScheme, 'dark')
  assert.equal(window.mobius.signal, undefined)
  assert.equal(projectPreviewSandbox(), 'allow-scripts')
  assert.match(html, /default-src 'none'/)
})
