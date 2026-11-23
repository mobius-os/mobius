/** Browser layout contracts for rendered chat content without a live workspace. */
import { test, expect } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import react from '../frontend/node_modules/@vitejs/plugin-react/dist/index.js'
import { createElement } from '../frontend/node_modules/react/index.js'
import { renderToStaticMarkup } from '../frontend/node_modules/react-dom/server.node.js'
import { createSsrRenderServer } from '../frontend/src/lib/__tests__/ssrRenderServer.mjs'

const chatView = new URL('../frontend/src/components/ChatView/', import.meta.url)
let vite
let StandardMarkdown
let QuestionCard

test.beforeAll(async () => {
  vite = await createSsrRenderServer({ configFile: false, plugins: [react()] })
  const markdown = await vite.ssrLoadModule(fileURLToPath(new URL('markdown/BlockRenderer.jsx', chatView)))
  StandardMarkdown = markdown.StandardMarkdown
  const questions = await vite.ssrLoadModule(fileURLToPath(new URL('QuestionCard.jsx', chatView)))
  QuestionCard = questions.default
})
test.afterAll(async () => { await vite?.close() })

test('chat tables wrap prose, retain alignment and widen for unbroken code', async ({ page }) => {
  const prose = 'The layout should wrap readable prose instead of squeezing it into tiny columns. '.repeat(3)
  const code = 'x'.repeat(100)
  const markdown = text => renderToStaticMarkup(createElement(StandardMarkdown, { text }))
  await page.setContent(`<style>body { width: 340px; font: 14px Arial; }</style>
    ${markdown('| A | B |\n| - | - |\n| one | two |')}
    ${markdown(`| Description | # |\n| --- | ---: |\n| ${prose} | 1 |\n| Short | 123456 |`)}
    ${markdown(`| Command | Result |\n| --- | --- |\n| \`${code}\` | OK |`)}`)
  await page.addStyleTag({ path: fileURLToPath(new URL('markdown.css', chatView)) })

  const [short, wrapped, wide] = await page.getByRole('table').evaluateAll(tables => (
    tables.map(table => {
      const [first, second] = table.tBodies[0].rows[0].cells
      const rect = element => element.getBoundingClientRect()
      const textRange = document.createRange()
      textRange.selectNodeContents(first)
      const lines = new Set([...textRange.getClientRects()].filter(r => r.width).map(r => Math.round(r.top)))
      const secondRange = document.createRange()
      secondRange.selectNodeContents(second)
      return {
        width: rect(table).width,
        available: table.parentElement.clientWidth,
        firstWidth: rect(first).width,
        secondWidth: rect(second).width,
        secondRightGap: rect(second).right - secondRange.getBoundingClientRect().right,
        lines: lines.size,
      }
    })
  ))
  expect(short.width).toBeCloseTo(short.available, 0)
  expect(wrapped.lines).toBeGreaterThan(1)
  expect(wrapped.firstWidth).toBeGreaterThan(wrapped.secondWidth * 2)
  expect(wrapped.secondRightGap).toBeLessThan(wrapped.secondWidth / 3)
  expect(wide.lines).toBe(1)
  expect(wide.width).toBeGreaterThan(wide.available)
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
