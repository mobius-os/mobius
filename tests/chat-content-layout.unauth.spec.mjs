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
let previousNodeEnv

test.beforeAll(async () => {
  previousNodeEnv = process.env.NODE_ENV
  vite = await createSsrRenderServer({ configFile: false, plugins: [react()] })
  const markdown = await vite.ssrLoadModule(fileURLToPath(new URL('markdown/BlockRenderer.jsx', chatView)))
  StandardMarkdown = markdown.StandardMarkdown
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
