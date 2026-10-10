/** Render the real Settings update view against isolated recovery snapshots. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const vite = await createServer({
  appType: 'custom', logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
const { PlatformUpdatesView } = await vite.ssrLoadModule('/src/components/SettingsView/PlatformUpdates.jsx')
after(() => vite.close())

const target = 'c'.repeat(40)

for (const state of ['finish', 'settling', 'conflict', 'restart']) {
  test(`unresolved recovery owns Settings actions while source is ${state}`, () => {
    const platform = {
      state: state === 'conflict' ? 'conflict' : 'ready',
      contained_upstream_sha: target,
      activation: state === 'restart'
        ? { level: 'server_restart', required_actions: ['server_restart'] }
        : { level: 'image_rebuild', required_actions: ['image_rebuild'] },
      unfinished_update: { stage: state === 'restart' ? 'finish' : state,
        action: state === 'restart' ? 'restart' : 'replace', cancellable: true },
      conflict_chat_id: state === 'conflict' ? 'some-chat' : null,
    }
    const update = {
      platform, cachedPlatform: null,
      rebuild: { state: 'needs_recovery', expected_sha: target, request_nonce: 'e'.repeat(32) },
      version: null, phase: 'idle', busy: false, reconnecting: false,
      error: '', errorCode: '', checkResult: '',
    }
    const html = renderToStaticMarkup(createElement(PlatformUpdatesView, {
      update, onOpenChat: () => {},
    }))
    assert.match(html, /The replacement needs recovery before another update can start/)
    assert.match(html, /The previous replacement still needs recovery in your deployment/)
    assert.match(html, /<button[^>]*>Ask Möbius<\/button>/)
    assert.match(html, /<button[^>]*>Check recovery status<\/button>/)
    assert.match(html, /<button class="settings__btn settings__btn--outline settings__btn--sm">Restart<\/button>/)
    assert.doesNotMatch(html, />(?:Finish update|Finish in chat|Review update|Cancel update|Keep this version|Withdraw request|Updating…|Checking…)</)
    assert.doesNotMatch(html, /The update is still running|Confirming the new container/)
    assert.doesNotMatch(html, /Your changes are ready|Restart to finish/)
  })
}

test('the recovery status control is disabled while checking', () => {
  const html = renderToStaticMarkup(createElement(PlatformUpdatesView, {
    update: {
      platform: { state: 'ready', contained_upstream_sha: target },
      rebuild: { state: 'needs_recovery', expected_sha: target },
      phase: 'checking', busy: true, reconnecting: false,
    },
  }))
  assert.match(html, /<button[^>]*disabled=""[^>]*>Checking recovery status…<\/button>/)
  assert.doesNotMatch(html, />(?:Review update|Cancel update|Keep this version|Withdraw request|Updating…)</)
})
