import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  needsStatusBarReload,
  saveThemeThenRefreshStatusBar,
} from '../statusBarThemeReload.js'

function fakeWindow({ installed, iosStandalone }) {
  const nav = {}
  if (iosStandalone !== undefined) nav.standalone = iosStandalone
  return {
    navigator: nav,
    matchMedia: () => ({ matches: installed }),
  }
}

const INSTALLED_IPHONE = fakeWindow({ installed: true, iosStandalone: true })
const IOS_IN_APP_BROWSER = fakeWindow({ installed: false, iosStandalone: true })
const INSTALLED_ANDROID = fakeWindow({ installed: true })
const SAFARI_TAB = fakeWindow({ installed: false, iosStandalone: false })

test('only the installed iPhone app needs a status-bar reload', () => {
  assert.equal(needsStatusBarReload(INSTALLED_IPHONE), true)
  assert.equal(needsStatusBarReload(IOS_IN_APP_BROWSER), false)
  assert.equal(needsStatusBarReload(INSTALLED_ANDROID), false)
  assert.equal(needsStatusBarReload(SAFARI_TAB), false)
})

test('a saved theme reloads the installed iPhone app after the save', async () => {
  const order = []
  const reloaded = await saveThemeThenRefreshStatusBar({
    save: async () => { order.push('save') },
    reload: async () => { order.push('reload') },
    win: INSTALLED_IPHONE,
  })
  assert.equal(reloaded, true)
  assert.deepEqual(order, ['save', 'reload'])
})

test('the in-app browser, Android and browser tabs never reload', async () => {
  for (const win of [IOS_IN_APP_BROWSER, INSTALLED_ANDROID, SAFARI_TAB]) {
    let reloads = 0
    const reloaded = await saveThemeThenRefreshStatusBar({
      save: async () => {},
      reload: async () => { reloads += 1 },
      win,
    })
    assert.equal(reloaded, false)
    assert.equal(reloads, 0)
  }
})

test('a failed save rejects without reloading', async () => {
  let reloads = 0
  await assert.rejects(saveThemeThenRefreshStatusBar({
    save: async () => { throw new Error('offline') },
    reload: async () => { reloads += 1 },
    win: INSTALLED_IPHONE,
  }), /offline/)
  assert.equal(reloads, 0)
})

test('a failed reload never turns a saved theme into an error', async () => {
  for (const reload of [
    async () => { throw new Error('navigation blocked') },
    () => { throw new Error('sync failure') },
  ]) {
    assert.equal(await saveThemeThenRefreshStatusBar({
      save: async () => {}, reload, win: INSTALLED_IPHONE,
    }), true)
  }
})
