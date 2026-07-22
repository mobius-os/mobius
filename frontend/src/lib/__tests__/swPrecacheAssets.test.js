import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  KATEX_ASSET_VERSION,
  KATEX_WOFF2_FILES,
  PDFJS_ASSET_VERSION,
  RETAINED_RUNTIME_ASSETS,
} from '../../sw-precache-assets.js'

const packageJson = JSON.parse(readFileSync(
  new URL('../../../package.json', import.meta.url),
  'utf8',
))
const dockerfile = readFileSync(
  new URL('../../../../Dockerfile', import.meta.url),
  'utf8',
)
const publicLogo = readFileSync(new URL('../../../public/moebius.png', import.meta.url))
const bundledLogo = readFileSync(new URL('../../assets/moebius.png', import.meta.url))

test('retained runtime precache covers the complete PDF and KaTeX URL payload', () => {
  const byUrl = new Map(RETAINED_RUNTIME_ASSETS.map(entry => [entry.url, entry]))

  assert.deepEqual(byUrl.get('/vendor/pdfjs/pdf.worker.mjs'), {
    url: '/vendor/pdfjs/pdf.worker.mjs',
    revision: `pdfjs-${PDFJS_ASSET_VERSION}`,
  })
  assert.equal(KATEX_WOFF2_FILES.length, 20)
  assert.equal(new Set(KATEX_WOFF2_FILES).size, KATEX_WOFF2_FILES.length)

  for (const base of [
    '/vendor/katex',
    `/vendor/katex@${KATEX_ASSET_VERSION}`,
  ]) {
    assert.ok(byUrl.has(`${base}/katex.min.css`), `${base} stylesheet`)
    for (const font of KATEX_WOFF2_FILES) {
      assert.ok(byUrl.has(`${base}/fonts/${font}`), `${base}/${font}`)
    }
  }

  assert.equal(RETAINED_RUNTIME_ASSETS.length, 43)
})

test('only stable aliases need explicit cache revisions', () => {
  for (const entry of RETAINED_RUNTIME_ASSETS) {
    if (entry.url.includes(`katex@${KATEX_ASSET_VERSION}`)) {
      assert.equal(entry.revision, null, entry.url)
    } else {
      assert.match(entry.revision, /^(pdfjs|katex)-\d/)
    }
  }
})

test('precache versions match the package graph and image asset copies', () => {
  assert.equal(packageJson.dependencies['pdfjs-dist'], PDFJS_ASSET_VERSION)
  assert.equal(packageJson.dependencies.katex, KATEX_ASSET_VERSION)
  assert.match(dockerfile, new RegExp(`pdfjs-dist@${PDFJS_ASSET_VERSION}`))
  assert.match(dockerfile, new RegExp(`pdfjs@${PDFJS_ASSET_VERSION}`))
  assert.match(dockerfile, new RegExp(`katex@${KATEX_ASSET_VERSION}`))
  assert.deepEqual(bundledLogo, publicLogo)
})
