/**
 * Shared chat-session helpers for the Playwright suite: creating a test chat
 * and sending a message.
 *
 * `createChat` uses the API-created chat (createTaggedChat, worker-tagged for
 * cleanup) plus the deep-link route instead of the drawer's New Chat button.
 * Specs that exercise the drawer's own flow keep that logic local.
 */
import { expect } from '@playwright/test'
import { createTaggedChat } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const painted = page => page.locator('[data-chat-surface="painted"]')

/**
 * Wait for the chat shell to have painted some recognizable surface: the
 * empty state, a scroll container, or the composer form. Call this right
 * after the initial navigation to BASE, before any chat exists.
 */
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

/**
 * Create a chat via the API and open it through the deep link, waiting for its
 * empty state. Returns the created chat (`{ id, title, ... }`).
 */
export async function createChat(page, label = '', { timeout = 8000 } = {}) {
  const chat = await createTaggedChat(page, label)
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  await expect(painted(page).locator('.chat__empty-wrap')).toBeVisible({ timeout })
  return chat
}

/**
 * Send a message as a fresh turn: fill the composer, press Enter once it
 * accepts a submit, and wait for this message's own user row. `settle` then
 * lets the send's layout pass (the pin) commit.
 */
export async function sendMessage(page, text, { timeout = 8000, settle = true } = {}) {
  const root = painted(page)
  const userRows = root.locator('.chat__msg--user')
  const previousCount = await userRows.count()
  await root.getByRole('textbox', { name: 'Message Möbius…' }).fill(text)
  await waitForComposerSendable(root)
  await page.keyboard.press('Enter')
  await expect(userRows).toHaveCount(previousCount + 1, { timeout })
  await expect(userRows.last()).toContainText(text)
  if (settle) {
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(r))
    ))
  }
}
