import { defineConfig } from '@playwright/test'

const AUTH_FILE = 'tests/.auth/state.json'
const isCI = !!process.env.CI

export default defineConfig({
  testDir: './tests',
  timeout: 60000,
  // CI retries once to absorb known chromium-headless flakes (e.g.
  // spacer test 9 times out under parallel load but passes solo).
  // No retries locally — flakes there should be investigated.
  retries: isCI ? 1 : 0,
  // CI emits both `list` (for stdout / Actions log) and `html` (for
  // the playwright-report/ artifact uploaded on failure). Locally
  // just stdout.
  reporter: isCI
    ? [['list'], ['html', { outputFolder: 'playwright-report', open: 'never' }]]
    : 'list',
  use: {
    headless: true,
    ignoreHTTPSErrors: true,
    // CI uses Playwright's bundled chromium; locally use the system
    // Chrome channel (faster startup, real browser).
    ...(isCI ? { browserName: 'chromium' } : { channel: 'chrome' }),
    // Capture diagnostics for failure analysis on CI.
    trace: isCI ? 'on-first-retry' : 'off',
    screenshot: isCI ? 'only-on-failure' : 'off',
  },
  projects: [
    { name: 'auth', testMatch: /auth\.setup\.mjs/ },
    {
      name: 'tests',
      testMatch: /\.spec\.mjs$/,
      dependencies: ['auth'],
      use: { storageState: AUTH_FILE },
    },
  ],
})
