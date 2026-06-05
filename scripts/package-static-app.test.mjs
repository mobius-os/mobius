import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import { packageStaticApp } from '../backend/scripts/package-static-app.mjs'

function tempRepo() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'mobius-static-package-'))
}

function write(file, content) {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, content)
}

test('packages a built static app and rewrites root asset refs', () => {
  const repo = tempRepo()
  const build = path.join(repo, 'build')
  write(path.join(build, 'index.html'), [
    '<!doctype html>',
    '<link rel="manifest" href="/manifest.json">',
    '<link rel="stylesheet" href="/static/css/main.css">',
    '<script src="/static/js/main.js"></script>',
  ].join('\n'))
  write(path.join(build, 'manifest.json'), '{"name":"demo"}')
  write(path.join(build, 'static/css/main.css'), [
    '@font-face{src:url(/static/media/font.woff2)}',
    '.local{background:url(../media/pixel.png)}',
    '.data{background:url(data:image/png;base64,AAAA)}',
  ].join('\n'))
  write(path.join(build, 'static/js/main.js'), 'console.log("demo")')
  write(path.join(build, 'static/js/main.js.map'), '{}')
  write(path.join(build, 'static/media/font.woff2'), 'font')
  write(path.join(build, 'static/media/pixel.png'), 'png')
  write(path.join(build, 'fonts.css'), '@font-face{src:url("/fonts/commando.ttf")}')
  write(path.join(build, 'fonts/commando.ttf'), 'font')

  const result = packageStaticApp({
    id: '3d-demo',
    name: '3D Demo',
    version: '1.0.0',
    description: 'A packaged static demo.',
    homepage: 'https://github.com/example/3d-demo',
    buildDir: build,
    outDir: repo,
    icon: 'icon.png',
  })

  assert.equal(result.assetCount, 8)
  assert.match(fs.readFileSync(path.join(build, 'index.html'), 'utf8'), /href="\.\/manifest\.json"/)
  assert.match(fs.readFileSync(path.join(build, 'index.html'), 'utf8'), /src="\.\/static\/js\/main\.js"/)
  assert.match(
    fs.readFileSync(path.join(build, 'static/css/main.css'), 'utf8'),
    /url\(\.\.\/media\/font\.woff2\)/,
  )
  assert.match(fs.readFileSync(path.join(build, 'fonts.css'), 'utf8'), /url\(\.\/fonts\/commando\.ttf\)/)

  const manifest = JSON.parse(fs.readFileSync(path.join(repo, 'mobius.json'), 'utf8'))
  assert.equal(manifest.id, '3d-demo')
  assert.equal(manifest.entry, 'index.jsx')
  assert.equal(manifest.static_assets['index.html'], 'build/index.html')
  assert.equal(manifest.static_assets['static/js/main.js'], 'build/static/js/main.js')
  assert.equal(manifest.static_assets['static/js/main.js.map'], undefined)
  assert.equal(manifest.permissions.cross_app_access, 'none')

  const wrapper = fs.readFileSync(path.join(repo, 'index.jsx'), 'utf8')
  assert.match(wrapper, /function Mobius3dDemoApp/)
  assert.match(wrapper, /\/app-assets\/by-id\/\$\{appId\}\/index\.html/)
  assert.match(wrapper, /\/app-assets\/3d-demo\/index\.html/)
})

test('fails when CSS references an unresolved local asset', () => {
  const repo = tempRepo()
  const build = path.join(repo, 'dist')
  write(path.join(build, 'index.html'), '<link rel="stylesheet" href="./main.css">')
  write(path.join(build, 'main.css'), '.missing{background:url(/missing.png)}')

  assert.throws(
    () => packageStaticApp({
      id: 'bad-static',
      name: 'Bad Static',
      version: '1.0.0',
      description: 'Broken asset graph.',
      buildDir: build,
      outDir: repo,
    }),
    /unresolved CSS url/,
  )
})

test('refuses to overwrite package files without force', () => {
  const repo = tempRepo()
  const build = path.join(repo, 'build')
  write(path.join(build, 'index.html'), '<script src="/static/js/main.js"></script>')
  write(path.join(build, 'static/js/main.js'), 'console.log("demo")')
  write(path.join(repo, 'mobius.json'), '{}')

  assert.throws(
    () => packageStaticApp({
      id: 'exists',
      name: 'Exists',
      version: '1.0.0',
      description: 'Already packaged.',
      buildDir: build,
      outDir: repo,
    }),
    /already exists/,
  )
  assert.equal(
    fs.readFileSync(path.join(build, 'index.html'), 'utf8'),
    '<script src="/static/js/main.js"></script>',
  )
})
