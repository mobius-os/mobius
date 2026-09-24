/**
 * Shared chat-session helpers for the Playwright suite: creating a test
 * chat and sending a message. Consolidates the near-duplicate
 * `setupChat`/`newChat`/`send` helpers that used to be hand-rolled per
 * spec file.
 *
 * `createChat` uses the API-created chat pattern (createTaggedChat +
 * explicit navigation to the deep-link route) rather than clicking
 * through the drawer's New Chat button. The API path is worker-tagged
 * for cleanup (see _chatTracker.mjs) and avoids UI-click races with the
 * drawer's open/close timing. Specs whose whole point is exercising the
 * drawer's own UI flow should keep that logic local instead of using
 * this helper.
 *
 * Follows the house style of the other tests/_*.mjs helper files:
 * plain exported functions, worker-tagged chat creation delegated to
 * _chatTracker.mjs, and no test-runner state of its own.
 */
import { expect } from '@playwright/test'
import { createTaggedChat } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

/**
 * Wait for the chat shell to have painted some recognizable surface —
 * the empty state, a scroll container, or the composer form. Call this
 * right after the initial navigation to BASE, before any chat exists.
 */
/**
 * Wait until the composer in `root` will accept a submit.
 *
 * The composer is revealed (and editable) before the chat's activation has
 * settled; until then ChatView deliberately ignores Enter, so a message typed
 * into an early composer stays in the draft and nothing is sent. The primary
 * action (Send with a draft, Voice input without one) carries that same
 * `submissionBlocked` gate as its disabled state, so it is the observable
 * "ready to send" signal. Resolves immediately if neither control is disabled.
 */
export async function waitForComposerSendable(root, { timeout = 10000 } = {}) {
  await expect(root.locator('.chat__send:disabled, .chat__mic:disabled'))
    .toHaveCount(0, { timeout })
}

export async function waitForChatShell(page, { timeout = 10000 } = {}) {
  await page.waitForFunction(
    () => !!(document.querySelector('[data-chat-surface="painted"] .chat__empty-wrap')
          || document.querySelector('[data-chat-surface="painted"] .chat__scroll')
          || document.querySelector('[data-chat-surface="painted"] .chat__form')),
    undefined,
    { timeout },
  )
}

/**
 * Create a chat via the API (tagged for this worker's cleanup — see
 * _chatTracker.mjs) and navigate the page onto it via the supported
 * deep link. Returns the created chat (`{ id, title, ... }`).
 *
 * `waitFor` selects what to wait for once the navigation lands:
 *   'empty-wrap' (default) — the painted empty-state wrapper is visible
 *   'form'                 — the painted composer form exists
 *   'none'                 — no post-navigation wait (caller does its own)
 */
export async function createChat(page, label = '', {
  base = BASE,
  waitFor = 'empty-wrap',
  timeout = 8000,
  mockProvider = true,
} = {}) {
  const chat = await createTaggedChat(page, label, { mockProvider })
  await page.goto(`${base}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  if (waitFor === 'empty-wrap') {
    await expect(page.locator('[data-chat-surface="painted"] .chat__empty-wrap'))
      .toBeVisible({ timeout })
  } else if (waitFor === 'form') {
    await page.waitForFunction(
      () => !!document.querySelector('[data-chat-surface="painted"] .chat__form'),
      undefined,
      { timeout },
    )
  }
  return chat
}

/**
 * Fill the composer and press Enter.
 *
 * `wait` selects the post-send synchronization signal:
 *   'user-message' (default) — the optimistic `.chat__msg--user` row renders
 *   'scroll'                 — the `.chat__scroll` container becomes visible
 *   'none'                   — no post-send wait (caller does its own)
 *
 * `settle` (default true) adds a two-frame `requestAnimationFrame` wait
 * after the signal so React's layout effects have flushed — the pattern
 * most specs already relied on before this dedup.
 */
export async function sendMessage(page, text, {
  scope,
  wait = 'user-message',
  timeout = 8000,
  settle = true,
} = {}) {
  const root = scope || page.locator('[data-chat-surface="painted"]')
  const input = root.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(text)
  await waitForComposerSendable(root)
  await page.keyboard.press('Enter')
  if (wait === 'user-message') {
    await expect(root.locator('.chat__msg--user').first()).toBeVisible({ timeout })
  } else if (wait === 'scroll') {
    await expect(root.locator('.chat__scroll')).toBeVisible({ timeout })
  }
  if (settle) {
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(r))
    ))
  }
}
