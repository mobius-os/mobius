/** Browser layout contract for the written answer box of a question card. */
import { test, expect } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import react from '../frontend/node_modules/@vitejs/plugin-react/dist/index.js'
import { createElement } from '../frontend/node_modules/react/index.js'
import { renderToStaticMarkup } from '../frontend/node_modules/react-dom/server.node.js'
import { createSsrRenderServer } from '../frontend/src/lib/__tests__/ssrRenderServer.mjs'

const chatView = new URL('../frontend/src/components/ChatView/', import.meta.url)
let vite
let QuestionCard
let previousNodeEnv

test.beforeAll(async () => {
  previousNodeEnv = process.env.NODE_ENV
  vite = await createSsrRenderServer({ configFile: false, plugins: [react()] })
  const questions = await vite.ssrLoadModule(fileURLToPath(new URL('QuestionCard.jsx', chatView)))
  QuestionCard = questions.default
})
test.afterAll(async () => {
  try {
    await vite?.close()
  } finally {
    // Creating the SSR server defaults the worker to development. Leave its
    // original environment for later specs that build browser fixtures.
    if (previousNodeEnv === undefined) delete process.env.NODE_ENV
    else process.env.NODE_ENV = previousNodeEnv
  }
})

test('an empty written answer starts at one line, grows and shrinks with text', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 })
  const html = renderToStaticMarkup(createElement(QuestionCard, {
    chatId: 'fixture',
    questionId: 'written-only',
    questions: [{ question: 'What should we do next?', options: [] }],
  }))
  await page.setContent(html)
  await page.addStyleTag({ path: fileURLToPath(new URL('QuestionCard.css', chatView)) })

  const answer = page.getByPlaceholder('Type your answer…')
  const size = () => answer.evaluate(element => {
    const { lineHeight, paddingTop, paddingBottom } = getComputedStyle(element)
    return {
      height: element.clientHeight,
      lines: (element.clientHeight - parseFloat(paddingTop) - parseFloat(paddingBottom)) / parseFloat(lineHeight),
      scrollHeight: element.scrollHeight,
    }
  })
  await expect(answer).toBeVisible()
  const empty = await size()
  expect(empty.lines).toBeLessThan(1.1)

  await answer.fill('First line\nSecond line\nThird line')
  const expanded = await size()
  expect(expanded.height).toBeGreaterThan(empty.height)
  expect(expanded.lines).toBeGreaterThan(2.9)

  await answer.fill('More detail\n'.repeat(20))
  const long = await size()
  expect(long.scrollHeight).toBeGreaterThan(long.height)

  await answer.clear()
  expect((await size()).height).toBe(empty.height)
})
