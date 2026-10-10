import { test, expect } from '@playwright/test'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { IDENTITY_STYLES } from '../frontend/src/components/SettingsView/identity/identity-styles.js'

// Bundle the actual account components in memory, not a copied modal. The
// test-only export leaves the production module's public surface unchanged.
const require = createRequire(new URL('../frontend/package.json', import.meta.url))
const accountPath = fileURLToPath(new URL('../frontend/src/components/SettingsView/identity/IdentityAccount.jsx', import.meta.url))
let bundle
async function accountBundle() {
  if (!bundle) bundle = (async () => {
    const { rolldown } = await import(require.resolve('rolldown'))
    const { transformWithOxc } = await import(require.resolve('vite'))
    const build = await rolldown({
      input: 'account-fixture',
      transform: { define: { 'process.env.NODE_ENV': JSON.stringify('production') } },
      plugins: [{
        name: 'isolated-account-fixture',
        resolveId(id) {
          if (id === 'account-fixture') return '\0account-fixture'
          if (id.endsWith('.css')) return '\0empty-style'
        },
        load(id) {
          if (id === '\0empty-style') return ''
          if (id === '\0account-fixture') return `
            import React from ${JSON.stringify(require.resolve('react'))};
            import { createRoot } from ${JSON.stringify(require.resolve('react-dom/client'))};
            import { RailwayConnectionModal, IdentityLoading, SignInModal } from ${JSON.stringify(accountPath)};
            window.mountAccount = (loading, connecting = false) => {
              window.accountRoot ||= createRoot(document.getElementById('fixture'));
              window.accountRoot.render(loading === 'sign-in' ? React.createElement(SignInModal, {
                token: 'isolated-fixture', onClose: () => {}, onSignedIn: () => {},
              }) : loading ? React.createElement(IdentityLoading) :
                React.createElement('div', { className: 'id-root id-root--settings' },
                  React.createElement(RailwayConnectionModal, {
                    token: 'isolated-fixture', connection: { account: 'Example', workspace: 'Example workspace', plan: 'hobby' },
                    connecting, onClose: () => {}, onReload: async () => {},
                    onChangeAccount: async () => false, onDisconnected: () => {},
                  })));
            };`
        },
        async transform(code, id) {
          if (!id.includes('/frontend/src/') || !/\.[jt]sx?$/.test(id)) return
          if (id === accountPath) code += '\nexport { RailwayConnectionModal, IdentityLoading };'
          code = code.replace(/import\.meta\.env(?:\?)?\.BASE_URL/g, "'/'")
            .replace(/import\.meta\.env\.DEV/g, 'false').replace(/import\.meta\.env\.MODE/g, "'test'")
          return (await transformWithOxc(code, id, {
            lang: id.endsWith('.jsx') ? 'jsx' : 'js', jsx: { runtime: 'automatic' }, sourcemap: false,
          })).code
        },
      }],
    })
    try {
      const { output } = await build.generate({ format: 'es', codeSplitting: false })
      return output.find(item => item.type === 'chunk').code
    } finally { await build.close() }
  })()
  return bundle
}

for (const width of [390, 1440]) {
  for (const theme of ['dark', 'light']) {
    for (const reducedMotion of ['reduce', 'no-preference']) {
      test(`account accessibility at ${width}px ${theme} ${reducedMotion}`, async ({ browser }) => {
        const context = await browser.newContext({ viewport: { width, height: 844 }, hasTouch: true, reducedMotion })
        try {
          const page = await context.newPage()
          // Every request is intercepted. No account, hosting, popup or paid
          // operation can reach the network from this production-component fixture.
          await page.route('**/*', route => {
            if (route.request().isNavigationRequest()) return route.fulfill({
              contentType: 'text/html', body: `<style>${IDENTITY_STYLES}</style><div data-theme="${theme}" class="settings"><div id="fixture"></div></div>`,
            })
            if (route.request().url().endsWith('/api/identity/railway/workspaces')) {
              return route.fulfill({ json: { current: 'one', workspaces: [
                { id: 'one', name: 'Example workspace' }, { id: 'two', name: 'Other workspace' },
              ] } })
            }
            return route.abort()
          })
          await page.goto('https://fixture.invalid/')
          await page.addScriptTag({ type: 'module', content: await accountBundle() })
          await page.evaluate(() => window.mountAccount(false))
          const dialog = page.getByRole('dialog', { name: 'Railway account' })
          await expect(dialog).toBeVisible()
          await expect(page.getByRole('combobox', { name: 'Railway workspace' })).toBeVisible()
          for (const control of await dialog.locator('button, a, select').all()) {
            const box = await control.boundingBox()
            expect(box.height).toBeGreaterThanOrEqual(44)
          }
          await page.getByRole('button', { name: 'Close', exact: true }).focus()
          await expect(page.getByRole('button', { name: 'Close', exact: true })).toBeFocused()
          // Do not dispatch account-changing actions: only inspect busy guards.
          await page.evaluate(() => window.mountAccount(false, true))
          for (const button of await dialog.locator('button').all()) await expect(button).toBeDisabled()
          await page.evaluate(() => window.mountAccount(true))
          await expect(page.getByRole('status')).toHaveText('Loading your account…')
          await expect(page.locator('main')).toHaveAttribute('aria-busy', 'true')
          const motion = await page.locator('.id-loading-hero').evaluate(node => ({
            section: getComputedStyle(node).animationName,
            shimmer: getComputedStyle(node.querySelector('.id-skeleton'), '::after').animationName,
          }))
          expect(motion).toEqual(reducedMotion === 'reduce'
            ? { section: 'none', shimmer: 'none' }
            : { section: 'id-rise', shimmer: 'id-skeleton-sweep' })
          // Additional real stylesheet states: online pulse and pending spinner
          // retain their visual/status nodes while reduced motion stops rotation.
          await page.locator('main').evaluate(node => node.insertAdjacentHTML('beforeend',
            '<span class="id-dot id-dot--online"></span><span class="id-spin"></span><button class="id-btn">Example action</button>'))
          for (const selector of ['.id-dot--online', '.id-spin']) {
            expect(await page.locator(selector).evaluate(node => getComputedStyle(node).animationName))
              .toBe(reducedMotion === 'reduce' ? 'none' : selector === '.id-spin' ? 'id-spin' : 'id-pulse')
          }
          expect(await page.locator('main > .id-btn').evaluate(node => getComputedStyle(node).transitionDuration))
            .toBe(reducedMotion === 'reduce' ? '0s' : '0.15s, 0.15s, 0.15s, 0.12s, 0.15s, 0.15s')
          await page.evaluate(() => window.mountAccount('sign-in'))
          await expect(page.locator('.id-provider').first()).toBeVisible()
          if (reducedMotion === 'reduce') {
            for (const control of await page.locator('.id-modal-backdrop button').all()) {
              expect(await control.evaluate(node => getComputedStyle(node).transitionDuration)).toBe('0s')
            }
          }
        } finally { await context.close() }
      })
    }
  }
}
