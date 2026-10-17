import test from 'node:test'
import assert from 'node:assert/strict'
import { useRef, renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import { useSettingsFocus } from '../../components/SettingsView/useSettingsFocus.js'

for (const page of ['account', 'provider']) {
  for (const section of ['github', 'ai-providers', 'models']) {
    test(`Settings request for ${section} leaves the retained ${page} page before focusing`, t => {
      const frames = new Map()
      let nextFrame = 0
      const oldRaf = globalThis.requestAnimationFrame
      const oldCancel = globalThis.cancelAnimationFrame
      globalThis.requestAnimationFrame = fn => { frames.set(++nextFrame, fn); return nextFrame }
      globalThis.cancelAnimationFrame = id => frames.delete(id)
      t.after(() => {
        if (oldRaf === undefined) delete globalThis.requestAnimationFrame; else globalThis.requestAnimationFrame = oldRaf
        if (oldCancel === undefined) delete globalThis.cancelAnimationFrame; else globalThis.cancelAnimationFrame = oldCancel
      })
      const focused = []
      const scrolled = []
      let mounted = false
      const node = {
        scrollIntoView() { assert.equal(mounted, true); scrolled.push(section) },
        focus() { assert.equal(mounted, true); focused.push(section) },
      }
      let detailPage = null
      let models = null
      const setDetailPage = next => { detailPage = next }
      const setManageModelsProvider = next => { models = next }
      const setAttentionSection = () => {}
      const view = renderHook(({ focusTarget }) => {
        const setupFocusRefs = useRef({})
        // React commits the updated page after the navigation effect's setters.
        mounted = !detailPage
        setupFocusRefs.current = mounted ? { github: node, 'ai-providers': node } : {}
        useSettingsFocus({ focusTarget, providerReady: true, detailPage, setDetailPage,
          setManageModelsProvider, setupFocusRefs, setAttentionSection })
      }, { focusTarget: null })
      t.after(() => view.unmount())
      detailPage = page === 'account' ? 'account' : 'codex'
      view.rerender({ focusTarget: null })
      assert.equal(mounted, false)
      const focusTarget = { section, nonce: 1 }
      view.rerender({ focusTarget })
      assert.equal(detailPage, null)
      view.rerender({ focusTarget })
      for (const [id, fn] of frames) { frames.delete(id); fn() }
      assert.deepEqual(focused, [section])
      assert.deepEqual(scrolled, [section])
      if (section === 'models') assert.equal(models, 'all')
      // A consumed request cannot reopen the overview when the owner opens detail.
      detailPage = page === 'account' ? 'account' : 'codex'
      view.rerender({ focusTarget })
      assert.equal(mounted, false)
    })
  }
}
