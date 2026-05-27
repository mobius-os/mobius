/**
 * Per-worker chat cleanup helper for the Playwright suite.
 *
 * Why this exists
 * ---------------
 * `auth.setup.mjs` wipes chats once before the whole suite. With
 * `workers: 4`, every worker creates chats in the same SQLite file
 * and they accumulate until the next run. Any test that lists chats
 * sees the union of in-flight work from all workers.
 *
 * Per-worker DATABASE_URL isolation would require N containers — the
 * backend builds its engine at import time, not per-request. That's a
 * lot of moving parts to add to docker-compose + CI for a contention
 * surface that, in practice, only really matters for the chat list
 * endpoint (the drawer filters `has_messages`, and Playwright mocks
 * `/messages`, so created chats stay drawer-invisible).
 *
 * Cheaper isolation: tag each test-created chat with a worker-scoped
 * title prefix, and bulk-delete that prefix at the end of each spec
 * file. SQLite stays single-DB; each worker effectively owns its own
 * title namespace. No backend changes, no container changes.
 *
 * Usage
 * -----
 *   import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'
 *   attachCleanup()             // at the top of the spec file
 *   ...
 *   await createTaggedChat(page) // inside a test — pulls workerIndex
 *                                // and test title from test.info()
 *
 * `createTaggedChat` is API-only (POST /api/chats with a worker-
 * prefixed title); spec-file `newChat` helpers call it first and then
 * drive the UI to land on the new chat the same way they used to.
 *
 * Rate-limit note
 * ---------------
 * `/api/auth/token` is throttled at 5/min. Cleanup across 4 workers
 * would blow through that, so we read the bearer token straight from
 * Playwright's saved `tests/.auth/state.json` (auth.setup.mjs writes
 * it into localStorage) and only fall back to the login endpoint if
 * the file is missing.
 */

import { test } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const USER = process.env.MOBIUS_USER || 'admin'
const PASS = process.env.MOBIUS_PASS || 'admin'

// Title prefix scheme: `__pw_w<workerIndex>_` so every spec file in
// the same worker shares the same namespace (worker reused across
// files in a Playwright run). The leading `__pw_` is the global tag
// — used by the cleanup pass to filter via startsWith.
const GLOBAL_PREFIX = '__pw_'
export function workerPrefix(workerIndex) {
  return `${GLOBAL_PREFIX}w${workerIndex}_`
}

/** Returns a unique title for this test in this worker. */
export function workerChatTitle(workerIndex, label = '') {
  const rand = Math.random().toString(36).slice(2, 8)
  const safeLabel = label.replace(/[^a-zA-Z0-9-]/g, '').slice(0, 20)
  return `${workerPrefix(workerIndex)}${rand}${safeLabel ? '_' + safeLabel : ''}`
}

// Worker-process-scoped token cache. The `/api/auth/token` endpoint
// is rate-limited (5/min); afterAll cleanup across 4 workers + a
// retry burst can blow through that and leave chats undeleted. We
// also read the token Playwright already cached in the storageState
// file as a first preference — no network call at all in the happy
// path.
let _cachedToken = null
async function getToken(request) {
  if (_cachedToken) return _cachedToken
  // 1. Try the saved storageState — auth.setup.mjs writes the token
  //    into localStorage at the BASE origin. Read it from disk.
  try {
    const fs = await import('fs/promises')
    const raw = await fs.readFile('tests/.auth/state.json', 'utf8')
    const state = JSON.parse(raw)
    for (const origin of state.origins || []) {
      for (const item of origin.localStorage || []) {
        if (item.name === 'token' && item.value) {
          _cachedToken = item.value
          return _cachedToken
        }
      }
    }
  } catch (_) { /* fall through to network */ }
  // 2. Fall back to the login endpoint (rate-limited; avoid).
  const res = await request.post(`${BASE}/api/auth/token`, {
    form: { username: USER, password: PASS },
    failOnStatusCode: false,
  })
  if (!res.ok()) return null
  const { access_token } = await res.json()
  _cachedToken = access_token
  return _cachedToken
}

/**
 * Create a chat via the API with a worker-tagged title.
 * Returns `{ id, title }`. The caller is responsible for navigating
 * the UI to it (e.g. by reload or by clicking the new-chat drawer
 * entry — `newChat` callers in the spec files already do this).
 *
 * Pulls `workerIndex` and the current test title from `test.info()`
 * so callers don't have to plumb `testInfo` through every helper
 * signature. Falls back to a no-title POST outside a test scope.
 */
export async function createTaggedChat(page, label = '') {
  let info = null
  try { info = test.info() } catch (_) { /* not in a test */ }
  const title = info
    ? workerChatTitle(info.workerIndex, label || info.title)
    : null
  const result = await page.evaluate(async (t) => {
    const token = localStorage.getItem('token')
    const res = await fetch('/api/chats', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(t ? { title: t } : {}),
    })
    if (!res.ok) return null
    return res.json()
  }, title)
  return result
}

/**
 * Delete every chat whose title starts with this worker's prefix.
 * Runs as `test.afterAll` per spec file (one cleanup pass per
 * worker-file pair). Best-effort: 4xx responses are swallowed so a
 * stale chat ID can't fail the suite.
 */
export async function cleanupWorkerChats(workerIndex, request) {
  const token = await getToken(request)
  if (!token) return
  const headers = { Authorization: `Bearer ${token}` }
  const listRes = await request.get(`${BASE}/api/chats`, { headers })
  if (!listRes.ok()) return
  const chats = await listRes.json()
  const prefix = workerPrefix(workerIndex)
  const mine = chats.filter(c => (c.title || '').startsWith(prefix))
  await Promise.all(mine.map(c =>
    request.delete(`${BASE}/api/chats/${c.id}`, {
      headers, failOnStatusCode: false,
    })
  ))
}

/**
 * Attach an `afterAll` cleanup hook to the current describe scope (or
 * the file root if called at the top level). Each Playwright worker
 * deletes its own prefixed chats once per spec file.
 */
export function attachCleanup() {
  test.afterAll(async ({ request }, testInfo) => {
    await cleanupWorkerChats(testInfo.workerIndex, request)
  })
}
