import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import StandaloneApp from '../../components/StandaloneApp/StandaloneApp.jsx'
import { useTranscriptNavigation } from '../../components/ChatView/markdown/AppBlockParts.jsx'
import { transcriptHostRequest } from '../../components/ChatView/markdown/appBlock.js'
import useAppIntentNavigation from '../../components/Shell/useAppIntentNavigation.js'

// A mini-app asks for Settings with moebius:open-settings wherever it runs.
// Each host that hands navigation to the workspace must keep the section.

const settingsRequest = section => ({ type: 'moebius:open-settings', section })

function pageAt(href) {
  const url = new URL(href)
  const location = { href: url.href, pathname: url.pathname, search: url.search }
  globalThis.window = { location }
  globalThis.history = { state: null }
  return location
}

// Run a component's or hook's real hooks once inside a server render and keep
// what it returns. Server rendering runs no effects, and returning null keeps
// the children (the app frame) unmounted.
function renderedOnce(run, wrap = element => element) {
  let output
  function Probe() {
    output = run()
    return null
  }
  renderToStaticMarkup(wrap(createElement(Probe)))
  return output
}

// The props of the first element in a rendered tree that carries `prop`.
function findProps(node, prop) {
  if (!node || typeof node !== 'object') return null
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findProps(child, prop)
      if (found) return found
    }
    return null
  }
  if (node.props?.[prop]) return node.props
  return findProps(node.props?.children, prop)
}

test('a standalone app page loads the workspace on the requested Settings section', () => {
  const location = pageAt('https://mobius.test/apps/social/')
  const client = new QueryClient()
  const tree = renderedOnce(
    () => StandaloneApp({ initialApp: { id: 7, name: 'Social', slug: 'social', updated_at: '1' } }),
    probe => createElement(QueryClientProvider, { client }, probe),
  )
  const { onHostRequest } = findProps(tree, 'onHostRequest')
  onHostRequest(7, settingsRequest('mobius-account'))
  assert.equal(location.href, '/shell/?settings=mobius-account')
  onHostRequest(7, settingsRequest('retired-section'))
  assert.equal(location.href, '/shell/?settings=ai-providers')
})

test('an app view in a chat transcript opens the requested Settings section in the shell', () => {
  pageAt('https://mobius.test/shell/?chat=c1')
  const internalNavs = []
  const navigate = renderedOnce(() => useTranscriptNavigation(url => internalNavs.push(url)))
  transcriptHostRequest(settingsRequest('mobius-account'), { navigate })
  transcriptHostRequest(settingsRequest('retired-section'), { navigate })
  assert.deepEqual(internalNavs.map(url => `${url.pathname}${url.search}`),
    ['/shell/?settings=mobius-account', '/shell/?settings=ai-providers'])

  const opened = []
  const { result } = renderHook(useAppIntentNavigation, {
    appsRef: { current: [] },
    refreshApps: async () => [],
    notifyShell: () => {},
    setAppIntents: () => {},
    navToRef: { current: () => {} },
    openSettingsSection: section => opened.push(section),
  })
  for (const url of internalNavs) result.current.handleChatInternalNav(url)
  assert.deepEqual(opened, ['mobius-account', 'ai-providers'])
})
