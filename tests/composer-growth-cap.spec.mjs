import { test, expect } from '@playwright/test'
import { attachCleanup, createTaggedChat } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test.use({ serviceWorkers: 'block' })
attachCleanup()

const LONG_DRAFT = 'A draft long enough to run the composer into its cap. '.repeat(12)

/** Geometry of the painted chat's composer, read from the live layout. */
function composerGeometry(page) {
  return page.evaluate(() => {
    const surface = document.querySelector('[data-chat-surface="painted"]')
    const chat = surface?.querySelector('.chat') || surface?.closest('.chat')
    const pill = surface?.querySelector('.chat__pill')
    if (!chat || !pill) return null
    return {
      chat: chat.getBoundingClientRect().height,
      pill: pill.getBoundingClientRect().height,
      input: surface.querySelector('.chat__input')?.getBoundingClientRect().height ?? 0,
      card: surface.querySelector('.chat__attach-card')?.getBoundingClientRect().height ?? 0,
      room: chat.style.getPropertyValue('--composer-room').trim(),
    }
  })
}

/** Read the geometry once it stops moving — the cap settles over a frame or
 *  two while the textarea grows and the foot's observers republish. */
async function settledGeometry(page) {
  let previous = null
  await expect.poll(async () => {
    const current = await composerGeometry(page)
    const stable = !!current && !!previous
      && current.pill === previous.pill
      && current.chat === previous.chat
    previous = current
    return stable
  }, { timeout: 8000 }).toBe(true)
  return previous
}

async function openNewChat(page, label) {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(page, label)
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  const painted = page.locator('[data-chat-surface="painted"]')
  const composer = painted.getByRole('textbox', { name: 'Message Möbius…' })
  await expect(composer).toBeVisible({ timeout: 8000 })
  return { painted, composer }
}

// A chat with no messages renders no scroll node on purpose. The growth cap
// used to be published from the scroll controller's geometry pass, which
// returns early exactly then — so on a brand new chat the cap was never
// published at all and `.chat__input` fell back to its `100dvh` default. iOS
// does not shrink `dvh` for the soft keyboard, which made the ORIGINAL bug
// survive in the flow most likely to hit it: open a new chat, attach a photo,
// start typing.
test('a new chat publishes the composer cap before any message exists', async ({ page }) => {
  await page.setViewportSize({ width: 412, height: 915 })
  const { painted } = await openNewChat(page, 'growth-cap-empty')

  // The empty state is the whole point: no transcript has rendered.
  await expect(painted.locator('.chat__scroll')).toHaveCount(0)

  await expect
    .poll(async () => (await composerGeometry(page))?.room ?? '', { timeout: 8000 })
    .toMatch(/^\d+px$/)
})

// The reserve's promise: whatever the chip tray occupies comes OUT of the text
// area's share instead of stacking on top of it, so attaching a file cannot
// grow the composer. This needs a short viewport — with plenty of room both
// states sit on the shared 280px ceiling and the property is untested.
test('an attached file comes out of the text share, not on top of it', async ({ page }) => {
  await page.setViewportSize({ width: 412, height: 480 })
  const { painted, composer } = await openNewChat(page, 'growth-cap-reserve')

  await composer.fill(LONG_DRAFT)
  const before = await settledGeometry(page)

  await painted.locator('input[type="file"]').setInputFiles({
    name: 'growth-cap.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('attachment'),
  })
  await expect(painted.getByRole('button', { name: 'Remove growth-cap.txt' }))
    .toBeVisible({ timeout: 8000 })

  const after = await settledGeometry(page)

  // Same pill height with and without the attachment — that is the reserve.
  expect(Math.abs(after.pill - before.pill)).toBeLessThanOrEqual(1)
  // And it still leaves the conversation the larger half. Before the cap, this
  // composer was a 280px text area plus a ~124px tray inside a ~430px pane.
  expect(after.pill / after.chat).toBeLessThan(0.6)
})

// The tray used to stay 96px tall regardless of the room. Once half the room
// fell below the fixed tray + the textarea's floor, clamp() could only honor
// the textarea floor and the composer again consumed nearly everything. Pin
// the review's landscape-keyboard geometry directly: the pending card gives
// room back before the conversation does.
test('a short keyboard room compacts the attachment before eclipsing the transcript', async ({ page }) => {
  await page.setViewportSize({ width: 844, height: 480 })
  const { painted, composer } = await openNewChat(page, 'growth-cap-short-room')

  // Headless Chromium cannot summon an iOS keyboard, so publish the 190px
  // visible band from the field geometry while leaving the shell itself roomy
  // enough for reliable controls. This exercises the live CSS layout rather
  // than restating its arithmetic in a source-reading unit test.
  await painted.locator('.chat').evaluate((chat) => {
    chat.style.setProperty('--composer-room', '190px')
  })
  await composer.fill(LONG_DRAFT)
  await painted.locator('input[type="file"]').setInputFiles({
    name: 'short-room.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('attachment'),
  })
  await expect(painted.getByRole('button', { name: 'Remove short-room.txt' }))
    .toBeVisible({ timeout: 8000 })

  const geometry = await settledGeometry(page)
  expect(geometry.card).toBeLessThan(96)
  expect(geometry.input).toBeGreaterThanOrEqual(24)
  expect(geometry.pill).toBeLessThanOrEqual(96)
})

// The shell applies VisualViewport geometry on an animation frame. A menu
// listening to the viewport event alone measures BEFORE that layout change;
// its form stays the same height, leaving its top controls clipped. This
// exercises the event/observer ordering, not just the height arithmetic.
for (const openFirst of [true, false]) {
  test(`tools stay reachable when keyboard ${openFirst ? 'opens after' : 'is open before'} the menu`, async ({ page }) => {
    await page.setViewportSize({ width: 402, height: 812 })
    const { painted, composer } = await openNewChat(page, `tools-keyboard-${openFirst}`)
    const trigger = painted.locator('.composer-plus > button')
    const panel = painted.getByRole('dialog', { name: 'Chat options', exact: true })
    await composer.focus()
    if (openFirst) await trigger.click()

    // Shrink only the visual viewport, like an overlay keyboard. The native
    // layout viewport stays 812px; the real shell hook must fit the chat pane.
    await page.evaluate(() => {
      Object.defineProperty(window.visualViewport, 'height', { configurable: true, value: 450 })
      window.visualViewport.dispatchEvent(new Event('resize'))
    })
    await expect.poll(async () => (await composerGeometry(page))?.chat ?? 812).toBeLessThan(450)
    if (!openFirst) await trigger.click()

    const fits = () => panel.evaluate(menu => {
      const rect = menu.getBoundingClientRect()
      const chat = menu.closest('.chat').getBoundingClientRect()
      const anchor = menu.parentElement.querySelector('button').getBoundingClientRect()
      return rect.height > 100 && rect.top >= chat.top + 7 && rect.bottom <= anchor.top - 7
    })
    await expect.poll(fits).toBe(true)
    await expect(composer).toBeFocused()

    // All menu content remains in ONE usable scrollport, including the first
    // Attach row after visiting the end of a long model/settings list.
    await panel.evaluate(menu => { menu.scrollTop = menu.scrollHeight })
    await panel.evaluate(menu => { menu.scrollTop = 0 })
    await expect(panel.getByRole('button', { name: /Attach files/ })).toBeInViewport()

    // A pane-only resize has no viewport event and still changes the room.
    await painted.locator('.chat').evaluate(chat => { chat.style.height = '330px' })
    await expect.poll(fits).toBe(true)
    await painted.locator('.chat').evaluate(chat => { chat.style.removeProperty('height') })
    await page.evaluate(() => {
      delete window.visualViewport.height
      window.visualViewport.dispatchEvent(new Event('resize'))
    })
    await expect.poll(fits).toBe(true)
    await expect(composer).toBeFocused()
  })
}
