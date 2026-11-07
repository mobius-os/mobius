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
      const setDetailPage = next => { detailPage = next }
      const setAttentionSection = () => {}
      const view = renderHook(({ focusTarget }) => {
        const setupFocusRefs = useRef({})
        // React commits the updated page after the navigation effect's setters.
        mounted = !detailPage
        setupFocusRefs.current = mounted ? { github: node, 'ai-providers': node } : {}
        useSettingsFocus({ focusTarget, providerReady: true, detailPage, setDetailPage,
          setupFocusRefs, setAttentionSection })
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
      // A consumed request cannot reopen the overview when the owner opens detail.
      detailPage = page === 'account' ? 'account' : 'codex'
      view.rerender({ focusTarget })
      assert.equal(mounted, false)
    })
  }
}

for (const [section, page] of [['mobius-account', 'mobius-account'], ['mobius-models', 'mobius']]) {
  test(`Settings request for ${section} opens its page instead of focusing the overview`, t => {
    const frames = new Map()
    const oldRaf = globalThis.requestAnimationFrame
    const oldCancel = globalThis.cancelAnimationFrame
    globalThis.requestAnimationFrame = fn => { frames.set(frames.size + 1, fn); return frames.size }
    globalThis.cancelAnimationFrame = id => frames.delete(id)
    t.after(() => {
      if (oldRaf === undefined) delete globalThis.requestAnimationFrame; else globalThis.requestAnimationFrame = oldRaf
      if (oldCancel === undefined) delete globalThis.cancelAnimationFrame; else globalThis.cancelAnimationFrame = oldCancel
    })
    let detailPage = 'codex'
    const view = renderHook(({ focusTarget }) => {
      const setupFocusRefs = useRef({})
      useSettingsFocus({ focusTarget, providerReady: true, detailPage, setDetailPage: next => { detailPage = next },
        setupFocusRefs, setAttentionSection: () => {} })
    }, { focusTarget: null })
    t.after(() => view.unmount())
    view.rerender({ focusTarget: { section, nonce: 1 } })
    assert.equal(detailPage, page)
    assert.equal(frames.size, 0)
  })
}

test('a models request opens the leading connected provider at its Chat models section, once', t => {
  const frames = new Map()
  const oldRaf = globalThis.requestAnimationFrame
  const oldCancel = globalThis.cancelAnimationFrame
  globalThis.requestAnimationFrame = fn => { frames.set(frames.size + 1, fn); return frames.size }
  globalThis.cancelAnimationFrame = id => frames.delete(id)
  t.after(() => {
    if (oldRaf === undefined) delete globalThis.requestAnimationFrame; else globalThis.requestAnimationFrame = oldRaf
    if (oldCancel === undefined) delete globalThis.cancelAnimationFrame; else globalThis.cancelAnimationFrame = oldCancel
  })
  const focused = []
  const attention = []
  const chatModels = { scrollIntoView() {}, focus() { focused.push('chat-models') } }
  let detailPage = null
  const pages = []
  const view = renderHook(({ focusTarget, providerReady, modelsProvider }) => {
    const setupFocusRefs = useRef({})
    setupFocusRefs.current = detailPage === 'claude' ? { 'chat-models': chatModels } : {}
    useSettingsFocus({ focusTarget, providerReady, modelsProvider, detailPage,
      setDetailPage: next => { pages.push(next); detailPage = next },
      setupFocusRefs, setAttentionSection: next => attention.push(next) })
  }, { focusTarget: null, providerReady: false, modelsProvider: null })
  t.after(() => view.unmount())
  const focusTarget = { section: 'models', nonce: 1 }
  // The provider list decides the page, so nothing moves before it is known.
  view.rerender({ focusTarget, providerReady: false, modelsProvider: null })
  assert.deepEqual(pages, [])
  view.rerender({ focusTarget, providerReady: true, modelsProvider: 'claude' })
  assert.deepEqual(pages, ['claude'])
  view.rerender({ focusTarget, providerReady: true, modelsProvider: 'claude' })
  for (const [id, fn] of frames) { frames.delete(id); fn() }
  assert.deepEqual(focused, ['chat-models'])
  assert.equal(attention[0], 'chat-models')
  // Reordering providers later never moves the owner to another page.
  view.rerender({ focusTarget, providerReady: true, modelsProvider: 'codex' })
  assert.deepEqual(pages, ['claude'])
})

test('a models request still focuses Chat models when the leading provider changes before the focus frame', t => {
  const frames = new Map()
  const oldRaf = globalThis.requestAnimationFrame
  const oldCancel = globalThis.cancelAnimationFrame
  globalThis.requestAnimationFrame = fn => { frames.set(frames.size + 1, fn); return frames.size }
  globalThis.cancelAnimationFrame = id => frames.delete(id)
  t.after(() => {
    if (oldRaf === undefined) delete globalThis.requestAnimationFrame; else globalThis.requestAnimationFrame = oldRaf
    if (oldCancel === undefined) delete globalThis.cancelAnimationFrame; else globalThis.cancelAnimationFrame = oldCancel
  })
  const focused = []
  const chatModels = { scrollIntoView() {}, focus() { focused.push(detailPage) } }
  let detailPage = null
  const pages = []
  const view = renderHook(({ focusTarget, modelsProvider }) => {
    const setupFocusRefs = useRef({})
    setupFocusRefs.current = detailPage ? { 'chat-models': chatModels } : {}
    useSettingsFocus({ focusTarget, providerReady: true, modelsProvider, detailPage,
      setDetailPage: next => { pages.push(next); detailPage = next },
      setupFocusRefs, setAttentionSection: () => {} })
  }, { focusTarget: null, modelsProvider: 'claude' })
  const focusTarget = { section: 'models', nonce: 1 }
  view.rerender({ focusTarget, modelsProvider: 'claude' })
  assert.deepEqual(pages, ['claude'])
  // Cached settings are replaced by a fresh read that puts Codex first.
  view.rerender({ focusTarget, modelsProvider: 'codex' })
  for (const [id, fn] of frames) { frames.delete(id); fn() }
  assert.deepEqual(pages, ['claude'])
  assert.deepEqual(focused, ['claude'])
  view.unmount()
})

test('a models request lands on AI providers when the provider list cannot be read', t => {
  const frames = new Map()
  const oldRaf = globalThis.requestAnimationFrame
  const oldCancel = globalThis.cancelAnimationFrame
  globalThis.requestAnimationFrame = fn => { frames.set(frames.size + 1, fn); return frames.size }
  globalThis.cancelAnimationFrame = id => frames.delete(id)
  t.after(() => {
    if (oldRaf === undefined) delete globalThis.requestAnimationFrame; else globalThis.requestAnimationFrame = oldRaf
    if (oldCancel === undefined) delete globalThis.cancelAnimationFrame; else globalThis.cancelAnimationFrame = oldCancel
  })
  const focused = []
  const aiProviders = { scrollIntoView() {}, focus() { focused.push('ai-providers') } }
  let detailPage = 'codex'
  const pages = []
  const view = renderHook(({ focusTarget, providersFailed }) => {
    const setupFocusRefs = useRef({})
    setupFocusRefs.current = detailPage ? {} : { 'ai-providers': aiProviders }
    useSettingsFocus({ focusTarget, providerReady: false, providersFailed, modelsProvider: 'claude', detailPage,
      setDetailPage: next => { pages.push(next); detailPage = next },
      setupFocusRefs, setAttentionSection: () => {} })
  }, { focusTarget: null, providersFailed: false })
  const focusTarget = { section: 'models', nonce: 1 }
  // Still loading: wait for the list.
  view.rerender({ focusTarget, providersFailed: false })
  assert.deepEqual(pages, [])
  // The first read fails with nothing cached.
  view.rerender({ focusTarget, providersFailed: true })
  assert.deepEqual(pages, [null])
  view.rerender({ focusTarget, providersFailed: true })
  for (const [id, fn] of frames) { frames.delete(id); fn() }
  assert.deepEqual(focused, ['ai-providers'])
  view.unmount()
})
