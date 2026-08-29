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
})

test('project preview policy precedes resources placed before a malformed head', () => {
  const remoteImage = '<img src="https://tracker.invalid/pixel"><head><title>Late head</title>'
  const result = safeProjectHtmlDocument(remoteImage)

  assert.equal(result.indexOf('Content-Security-Policy') < result.indexOf(remoteImage), true)
  assert.match(result, /^<meta http-equiv="Content-Security-Policy"/)
})

test('preview storage waits for the parent handshake and has a bounded failure', () => {
  const document = safeProjectHtmlDocument('<main>Preview</main>')
  assert.match(document, /mobius:project-preview-storage-connected/)
  assert.match(document, /if \(connected\) parent\.postMessage/)
  assert.match(document, /did not connect/)
  assert.match(document, /clearTimeout\(request\.timeout\)/)
})

test('project HTML preview runs scripts without granting origin or navigation access', () => {
  assert.equal(projectPreviewSandbox(), 'allow-scripts')
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
