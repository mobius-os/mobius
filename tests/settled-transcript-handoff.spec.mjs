import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'
import { testChatAgentSettings } from './_chatTestPrerequisites.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8000'

attachCleanup()
test.use({
  serviceWorkers: 'block',
  viewport: { width: 1512, height: 861 },
  deviceScaleFactor: 2,
})

function seedHistory() {
  return Array.from({ length: 20 }, (_, index) => {
    const role = index % 2 === 0 ? 'user' : 'assistant'
    const content = role === 'user'
      ? `Earlier question ${index / 2 + 1}`
      : `### Earlier result\n\n${'Historical response text. '.repeat(14)}`
    return {
      role,
      content,
      blocks: [{ type: 'text', content }],
      ts: 1_780_000_000_000 + index,
      ...(role === 'user' ? { cid: `history-${index}` } : {}),
    }
  })
}

async function installStreamMock(page, firstItems) {
  await page.addInitScript(({ initialItems }) => {
    const realFetch = window.fetch.bind(window)
    let streamIndex = 0
    window.fetch = (input, init) => {
      const url = String(input?.url || input)
      if (!/\/api\/chats\/[^/]+\/stream$/.test(url)) {
        return realFetch(input, init)
      }
      const current = streamIndex++
      const items = current === 0
        ? initialItems
        : [{ type: 'thinking', content: 'Checking the follow-up.' }]
      const delay = current === 0 ? 0 : 300
      const doneAfter = current === 0 ? 420 : 1800
      const encoder = new TextEncoder()
      return Promise.resolve(new Response(new ReadableStream({
        start(controller) {
          setTimeout(() => controller.enqueue(encoder.encode(
            `data: ${JSON.stringify({ type: 'stream_snapshot', items })}\n\n`,
          )), delay + 5)
          setTimeout(() => controller.enqueue(encoder.encode(
            `data: ${JSON.stringify({ type: 'catch_up_done' })}\n\n`,
          )), delay + 20)
          setTimeout(() => {
            controller.enqueue(encoder.encode(
              `data: ${JSON.stringify({ type: 'done' })}\n\n`,
            ))
            controller.close()
          }, delay + doneAfter)
        },
      }), {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }))
    }
  }, { initialItems: firstItems })
}

async function mountScenario(page) {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(page, 'settled-transcript-handoff')
  expect(chat?.id).toBeTruthy()

  const mediaPath = `/api/chats/${chat.id}/media/handoff.png`
  const finalText = [
    '### Verification result',
    '',
    'The complete response includes a tall visual proof.',
    '',
    `![Tall proof](${mediaPath})`,
  ].join('\n')
  const liveItems = [
    { type: 'text', content: 'I’ll verify the handoff first.' },
    {
      type: 'tool',
      tool: 'Bash',
      input: 'run verification',
      output: 'passed',
      status: 'done',
      tool_use_id: 'handoff-tool',
    },
    { type: 'text', content: finalText },
  ]
  const settledAssistant = {
    role: 'assistant',
    content: liveItems.filter(item => item.type === 'text')
      .map(item => item.content).join('\n\n'),
    blocks: liveItems,
    ts: 1_787_000_000_100,
    media_dimensions: {
      [mediaPath]: { width: 320, height: 900 },
    },
  }

  let messages = seedHistory()
  let running = false
  let sendCount = 0

  await installStreamMock(page, liveItems)

  await page.route(new RegExp(`${mediaPath}$`), route => route.fulfill({
    status: 200,
    contentType: 'image/svg+xml',
    body: '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="900" viewBox="0 0 320 900"><rect width="320" height="900" fill="#8b5cf6"/></svg>',
  }))
  await page.route(new RegExp(`/api/chats/${chat.id}/runtime(?:\\?.*)?$`), route => (
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        running,
        active_goal_objective: null,
        pending_messages: [],
        pending_question_id: null,
      }),
    })
  ))
  await page.route(new RegExp(`/api/chats/${chat.id}(?:\\?.*)?$`), async route => {
    if (route.request().method() !== 'GET') return route.continue()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: chat.id,
        title: 'Settled transcript handoff',
        messages,
        total: messages.length,
        offset: 0,
        running,
        pending_messages: [],
        pending_question_id: null,
        provider: 'codex',
        ...testChatAgentSettings(),
      }),
    })
  })
  await page.route(new RegExp(`/api/chats/${chat.id}/messages$`), async route => {
    const request = route.request().postDataJSON()
    sendCount += 1
    running = true
    const message = {
      role: 'user',
      content: request.content,
      blocks: [{ type: 'text', content: request.content }],
      ts: 1_787_000_000_000 + sendCount,
      cid: request.cid,
    }
    messages = [...messages, message]
    if (sendCount === 1) {
      setTimeout(() => {
        messages = [...messages, settledAssistant]
        running = false
      }, 320)
    }
    await new Promise(resolve => setTimeout(resolve, 100))
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'started', message }),
    })
  })

  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  const surface = page.locator('[data-chat-surface="painted"]')
  const scroll = surface.locator('.chat__scroll')
  await expect(scroll).toBeVisible({ timeout: 30000 })
  await expect(surface.locator('.chat__msg')).toHaveCount(20, { timeout: 30000 })

  return {
    chat,
    surface,
    scroll,
    settledAssistant,
  }
}

async function prepareSendFromBottom(page, surface, text) {
  await page.evaluate(() => {
    const scroll = document.querySelector(
      '[data-chat-surface="painted"] .chat__scroll',
    )
    scroll.scrollTop = scroll.scrollHeight
  })
  const input = surface.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(text)
  await page.waitForFunction(() => {
    const scroll = document.querySelector(
      '[data-chat-surface="painted"] .chat__scroll',
    )
    return scroll
      && scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 4
  })
}

async function sendFromBottom(page, surface, text) {
  await prepareSendFromBottom(page, surface, text)
  await page.keyboard.press('Enter')
}

async function sampleNextSend(page, surface, text, settledAssistantTs) {
  await prepareSendFromBottom(page, surface, text)
  const priorCount = await surface.locator('.chat__msg--user').count()
  await page.evaluate(() => {
    window.__handoffFrames = []
    window.__handoffSampling = true
    const sample = () => {
      const surface = document.querySelector('[data-chat-surface="painted"]')
      const scroll = surface?.querySelector('.chat__scroll')
      const users = surface?.querySelectorAll('.chat__msg--user') || []
      const row = users[users.length - 1]
      const sr = scroll?.getBoundingClientRect()
      const rr = row?.getBoundingClientRect()
      window.__handoffFrames.push({
        users: users.length,
        top: sr && rr ? rr.top - sr.top : null,
      })
      if (window.__handoffSampling) requestAnimationFrame(sample)
    }
    requestAnimationFrame(sample)
  })

  await page.keyboard.press('Enter')
  await page.waitForFunction(ts => {
    const rows = document.querySelectorAll(
      '[data-chat-surface="painted"] .chat__msg--assistant',
    )
    return [...rows].some(row => row.dataset.key === `assistant-${ts}`)
  }, settledAssistantTs, { timeout: 10000 })
  await page.evaluate(() => new Promise(resolve => (
    requestAnimationFrame(() => requestAnimationFrame(resolve))
  )))
  return page.evaluate(previousCount => {
    window.__handoffSampling = false
    return (window.__handoffFrames || [])
      .filter(frame => frame.users > previousCount && frame.top != null)
      .map(frame => frame.top)
  }, priorCount)
}

test('an authoritative settled-answer handoff cannot move a pinned send', async ({ page }) => {
  const scenario = await mountScenario(page)
  await sendFromBottom(page, scenario.surface, 'First message')
  await expect(scenario.surface.getByText('Verification result', { exact: false }))
    .toBeVisible({ timeout: 10000 })
  // Finish the first stream while its detailed live row is still mounted. The
  // server already holds the compact settled projection, but the next send's
  // authoritative read is what hands the rendered row over to that source.
  await expect(scenario.surface.locator('.chat__stop')).toHaveCount(0, {
    timeout: 10000,
  })
  await page.waitForFunction(ts => {
    const rows = document.querySelectorAll(
      '[data-chat-surface="painted"] .chat__msg--assistant',
    )
    const key = rows[rows.length - 1]?.dataset.key
    return key && key !== `assistant-${ts}`
  }, scenario.settledAssistant.ts)

  // Sample every painted frame across that source handoff. The newly sent row
  // must stay at its semantic pin rather than wait for a later resize repair.
  const tops = await sampleNextSend(
    page,
    scenario.surface,
    'Second message',
    scenario.settledAssistant.ts,
  )
  expect(tops.length).toBeGreaterThan(0)
  expect(Math.max(...tops)).toBeLessThanOrEqual(12)
  expect(Math.min(...tops)).toBeGreaterThanOrEqual(-2)
})
