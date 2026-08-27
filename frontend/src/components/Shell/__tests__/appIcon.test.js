import test from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import AppIcon from '../../AppIcon.jsx'
import {
  appIconIsReady,
  appIconUrl,
  appInitials,
  preloadAppIcons,
} from '../../appIcon.js'

function iconImages(element) {
  return element.props.children
    .flat(Infinity)
    .filter(child => child?.type === 'img')
}

function displayedIconUrl(element) {
  return iconImages(element)
    .find(image => image.props.className === 'app-icon__image--displayed')
    ?.props.src || null
}

test('app initials remain useful when custom artwork is missing', () => {
  assert.equal(appInitials('Beat Machine'), 'BM')
  assert.equal(appInitials('Atlas'), 'AT')
  assert.equal(appInitials('---'), 'A')
})

test('installed app chrome sizes the canonical icon reference from AppOut', () => {
  const app = {
    id: 42,
    slug: 'example-app',
    icon_url: '/api/apps/42/icon?v=2026-07-27T03%3A00%3A00%2B00%3A00',
  }

  assert.equal(
    appIconUrl(app, 64),
    '/api/apps/42/icon?v=2026-07-27T03%3A00%3A00%2B00%3A00&size=64',
  )
  assert.equal(
    appIconUrl(app),
    '/api/apps/42/icon?v=2026-07-27T03%3A00%3A00%2B00%3A00&size=128',
  )
  assert.equal(
    appIconUrl(app, null),
    '/api/apps/42/icon?v=2026-07-27T03%3A00%3A00%2B00%3A00',
    'callers can request the accepted original without rebuilding its identity',
  )
  assert.equal(appIconUrl({ id: 7, icon_url: null }), null)
  assert.equal(appIconUrl({ id: 7 }), null)
  assert.equal(appIconUrl(null), null)
})

test('shell icon warming preserves app order and removes duplicate asset work', async () => {
  const apps = [
    { icon_url: '/api/apps/1/icon?v=a' },
    { icon_url: '/api/apps/2/icon?v=b' },
    { icon_url: '/api/apps/1/icon?v=a' },
    { icon_url: null },
  ]
  const requested = []
  class FakeImage {
    set src(value) {
      requested.push(value)
      queueMicrotask(() => this.onload())
    }
  }
  await preloadAppIcons(apps, { ImageCtor: FakeImage })
  assert.deepEqual(requested, [
    '/api/apps/1/icon?v=a&size=128',
    '/api/apps/2/icon?v=b&size=128',
  ])
})

test('preloaded artwork is decoded and ready for the launcher first render', async () => {
  let decodeCount = 0
  class FakeImage {
    set src(value) {
      this.value = value
      queueMicrotask(() => this.onload())
    }
    decode() {
      decodeCount += 1
      return Promise.resolve()
    }
  }

  const app = { icon_url: '/api/apps/9/icon?v=ready' }
  const url = `${app.icon_url}&size=128`
  const result = await preloadAppIcons([app], { ImageCtor: FakeImage })
  assert.deepEqual(result, [true])
  assert.equal(decodeCount, 1)
  assert.equal(appIconIsReady(url), true)
})

test('an app update keeps painted artwork until its replacement loads', () => {
  const oldApp = { id: 42, icon_url: '/api/apps/42/icon?v=old' }
  const nextApp = { id: 42, icon_url: '/api/apps/42/icon?v=next' }
  const retryApp = { id: 42, icon_url: '/api/apps/42/icon?v=retry' }
  const { result, rerender } = renderHook(AppIcon, {
    item: oldApp,
    label: 'Example',
  })

  assert.equal(displayedIconUrl(result.current), null)
  iconImages(result.current)[0].props.onLoad()
  assert.equal(displayedIconUrl(result.current), `${oldApp.icon_url}&size=128`)

  rerender({ item: nextApp, label: 'Example' })
  assert.equal(
    displayedIconUrl(result.current),
    `${oldApp.icon_url}&size=128`,
    'the already-painted node remains visible while the new URL loads',
  )
  assert.deepEqual(
    iconImages(result.current).map(image => image.props.src),
    [`${oldApp.icon_url}&size=128`, `${nextApp.icon_url}&size=128`],
  )

  const candidate = iconImages(result.current)[1]
  candidate.props.onError()
  assert.equal(
    displayedIconUrl(result.current),
    `${oldApp.icon_url}&size=128`,
    'a failed replacement cannot blank known-good artwork',
  )

  rerender({ item: retryApp, label: 'Example' })
  iconImages(result.current)[1].props.onLoad()
  assert.equal(displayedIconUrl(result.current), `${retryApp.icon_url}&size=128`)
  assert.equal(iconImages(result.current).length, 1)
})

test('a superseded icon load cannot replace the current candidate', () => {
  const oldApp = { id: 42, icon_url: '/api/apps/42/icon?v=old-race' }
  const supersededApp = { id: 42, icon_url: '/api/apps/42/icon?v=superseded' }
  const currentApp = { id: 42, icon_url: '/api/apps/42/icon?v=current' }
  const { result, rerender } = renderHook(AppIcon, {
    item: oldApp,
    label: 'Example',
  })

  iconImages(result.current)[0].props.onLoad()
  rerender({ item: supersededApp, label: 'Example' })
  const staleLoad = iconImages(result.current)[1].props.onLoad
  rerender({ item: currentApp, label: 'Example' })

  staleLoad()
  assert.equal(displayedIconUrl(result.current), `${oldApp.icon_url}&size=128`)
  iconImages(result.current)[1].props.onLoad()
  assert.equal(displayedIconUrl(result.current), `${currentApp.icon_url}&size=128`)
})

test('artwork from a different app is never retained as an update fallback', () => {
  const first = { id: 1, icon_url: '/api/apps/1/icon?v=ready' }
  const second = { id: 2, icon_url: '/api/apps/2/icon?v=pending' }
  const { result, rerender } = renderHook(AppIcon, { item: first, label: 'First' })

  iconImages(result.current)[0].props.onLoad()
  assert.equal(displayedIconUrl(result.current), `${first.icon_url}&size=128`)

  rerender({ item: second, label: 'Second' })
  assert.equal(displayedIconUrl(result.current), null)
  assert.deepEqual(
    iconImages(result.current).map(image => image.props.src),
    [`${second.icon_url}&size=128`],
  )
})
