import { defineConfig } from '@playwright/test'
import { existsSync, readdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

const AUTH_FILE = process.env.MOBIUS_AUTH_FILE || 'tests/.auth/state.json'
const isCI = !!process.env.CI

if (!isCI && process.env.MOBIUS_LOCAL_E2E !== '1') {
  throw new Error(
    'Local Playwright E2E is opt-in because it builds a disposable Möbius ' +
    'backend and database. Prefer the GitHub PR checks. For a focused local ' +
    'run, use scripts/playwright-local.sh --allow-local-e2e <spec or --grep>.'
  )
}

/** Resolve the Chrome-for-Testing binary already installed for agent-browser.
 * Möbius images ship that browser even when the conventional system `chrome`
 * channel is absent. Falling through to Playwright's channel lookup made every
 * local browser contract fail before launch, so scroll changes were routinely
 * declared green from unit tests alone. An explicit env override remains first
 * for ordinary contributor workstations; bare local installs still fall back
 * to the standard `chrome` channel. */
function localChromeExecutable() {
  const explicit = process.env.MOBIUS_PLAYWRIGHT_EXECUTABLE
    || process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
  if (explicit && existsSync(explicit)) return explicit

  const roots = [
    '/opt/agent-browser/browsers',
    join(homedir(), '.agent-browser', 'browsers'),
  ]
  for (const root of roots) {
    let dirs = []
    try {
      dirs = readdirSync(root, { withFileTypes: true })
        .filter(entry => entry.isDirectory() && entry.name.startsWith('chrome-'))
        .map(entry => entry.name)
        .sort((a, b) => b.localeCompare(a, undefined, { numeric: true }))
    } catch {
      continue
    }
    for (const dir of dirs) {
      const executable = join(root, dir, 'chrome')
      if (existsSync(executable)) return executable
    }
  }
  return null
}

const localChrome = isCI ? null : localChromeExecutable()

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
  // (the streaming and queue tests in particular). Across files is safe
  // because the run uses a disposable database and specs operate on explicit
  // fixtures they create or mock.
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
    // CI uses Playwright's bundled chromium. Möbius instances use the browser
    // already shipped for agent-browser; contributor machines without it fall
    // back to the conventional system Chrome channel.
    ...(isCI
      ? { browserName: 'chromium' }
      : localChrome
        ? {
            browserName: 'chromium',
            launchOptions: { executablePath: localChrome },
          }
        : { channel: 'chrome' }),
    // Capture diagnostics for failure analysis on CI.
    trace: isCI ? 'on-first-retry' : 'off',
    screenshot: isCI ? 'only-on-failure' : 'off',
  },
  projects: [
    {
      name: 'auth',
      testMatch: /auth\.setup\.mjs/,
      // Block the service worker for setup only. On a fresh context the first
      // /sw.js install activates + clientsClaim()s the page ~1s in, and
      // index.html's watchdog does a one-time location.reload() to adopt it.
      // That reload lands mid-setup and wipes the just-filled login form — a
      // ~50% flake (the form submits empty; native "Please fill out this
      // field" blocks login). Setup only needs to log in and save state; it
      // never exercises the SW, so blocking it makes the page load once and
      // stay stable. The SW-behavior specs keep their own (allowed) contexts.
      use: { serviceWorkers: 'block' },
      // Auth setup races a 15s rAF poll for shell readiness — if the
      // shell genuinely never reaches ready, additional retries just
      // multiply that wait. One retry is enough to absorb single-flake
      // jitter (transient SQLite or CLI-auth races) without triple-
      // billing wall-clock when the underlying issue is real. Cascade-
      // fail risk is mitigated upstream by workers=2 reducing the
      // contention that produced most auth flakes in the first place.
      retries: 1,
    },
    {
      name: 'tests',
      testMatch: /\.spec\.mjs$/,
      // Exclude SSE-timing specs from this project — they get their
      // own project below with retries=0 so a 50%-flake regression
      // can't be papered over by the global retries=1.
      testIgnore: /(stream-reconnect|handleStop-sync-ordering)\.spec\.mjs$/,
      dependencies: ['auth'],
      use: { storageState: AUTH_FILE },
    },
    {
      // SSE-timing-sensitive specs: a 50%-pass-rate regression here
      // (e.g. a race in disconnect({clearStreaming:true}) or queue/
      // stop ordering) would be masked by the global retries=1.
      // Pin to retries=0 so the regression surfaces on the first
      // attempt — these tests' contracts are explicitly about timing
      // windows and shouldn't be retried into green.
      name: 'tests-timing',
      testMatch: /(stream-reconnect|handleStop-sync-ordering)\.spec\.mjs$/,
      dependencies: ['auth'],
      use: { storageState: AUTH_FILE },
      retries: 0,
    },
  ],
})
