import { defineConfig } from '@playwright/test'

const AUTH_FILE = 'tests/.auth/state.json'
const isCI = !!process.env.CI

export default defineConfig({
  testDir: './tests',
  timeout: 60000,
  // One retry both locally and on CI to absorb cascade-under-load
  // jitter on the single-instance test container — multiple workers
  // sharing one mobius-test sometimes produce ECONNRESET on cleanup
  // or a SSE timing miss that the next attempt sails through. Real
  // regressions still surface (they fail both attempts) and local
  // pass-rate matches CI without a 30%-flake tax during fast iter.
  // Auth setup gets its own retries override below.
  retries: 1,
  // Per-file parallelism. Within a file, tests stay sequential
  // because many spec files share state via send-then-read patterns
  // (the streaming and queue tests in particular). Across files
  // is safe: auth.setup.mjs wipes chats before the suite starts,
  // and each spec file's tests operate on chats they create.
  fullyParallel: false,
  // Match CI's worker count locally too. The previous local=4 setting
  // saturated mobius-test (single SQLite, single uvicorn) and produced
  // cascade-fail noise — different specs failed each run depending on
  // which two collided on SSE timing or a write transaction. 2 workers
  // adds ~30s wall-clock to a full suite run but eliminates the noise
  // entirely; real regressions surface deterministically.
  workers: 2,
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
    {
      name: 'auth',
      testMatch: /auth\.setup\.mjs/,
      // Auth setup races a 15s rAF poll for shell readiness and is
      // the single point of failure for every spec — when it flakes,
      // 12 spec files cascade-fail with misleading "auth state
      // missing" errors. Override the global retries=0 (local) and
      // retries=1 (CI) so the setup absorbs its own jitter without
      // polluting test results.
      retries: 2,
    },
    {
      name: 'tests',
      testMatch: /\.spec\.mjs$/,
      dependencies: ['auth'],
      use: { storageState: AUTH_FILE },
    },
  ],
})
