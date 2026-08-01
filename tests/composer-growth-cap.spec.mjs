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
