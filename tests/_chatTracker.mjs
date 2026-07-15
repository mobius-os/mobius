/**
 * Per-worker chat cleanup helper for the Playwright suite.
 *
 * Why this exists
 * ---------------
 * Each run already has its own backend and database. Within that run, this
 * helper records the exact IDs created by each worker and deletes only those
 * IDs. It never lists the account and never infers ownership from titles.
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
 * Playwright's configured auth-state file (auth.setup.mjs writes the token
 * into localStorage) and only fall back to the login endpoint if it is missing.
 */

import { test } from '@playwright/test'
import {
  drainCreatedChats,
  registerCreatedChats,
} from './_chatFixtureRegistry.mjs'

export { registerCreatedChats } from './_chatFixtureRegistry.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const USER = process.env.MOBIUS_USER || 'admin'
const PASS = process.env.MOBIUS_PASS || 'admin'

// Human-readable title tags remain useful in diagnostics, but cleanup never
// relies on them.
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
    const authFile = process.env.MOBIUS_AUTH_FILE || 'tests/.auth/state.json'
    const raw = await fs.readFile(authFile, 'utf8')
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
  if (info && result?.id) registerCreatedChats(info.workerIndex, result.id)
  return result
}

/**
 * Delete only chats registered by this worker. Best-effort: 4xx responses
 * are swallowed so a stale fixture ID cannot fail the suite.
 */
export async function cleanupWorkerChats(workerIndex, request) {
  const ids = drainCreatedChats(workerIndex)
  if (ids.length === 0) return
  const token = await getToken(request)
  if (!token) return
  const headers = { Authorization: `Bearer ${token}` }
  await Promise.all(ids.map(id =>
    request.delete(`${BASE}/api/chats/${id}`, {
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
