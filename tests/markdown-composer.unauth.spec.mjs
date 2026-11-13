/** Native browser contracts for the real Markdown composer, isolated from owner data and providers. */
import { test, expect } from '@playwright/test'
import { build } from '../frontend/node_modules/vite/dist/node/index.js'
import react from '../frontend/node_modules/@vitejs/plugin-react/dist/index.js'
import { composerValue, composerSelection, setComposerSelection } from './_composer.mjs'
import { fileURLToPath } from 'node:url'
const root = fileURLToPath(new URL('../frontend/', import.meta.url))
let assets

test.beforeAll(async () => {
  const result = await build({
    configFile: false, root, plugins: [react()], logLevel: 'silent', define: { 'process.env.NODE_ENV': JSON.stringify('production') },
    build: { write: false, minify: false, lib: {
      entry: `${root}/src/components/ChatView/__tests__/markdownComposer.fixture.jsx`,
      name: 'ComposerFixture', formats: ['es'],
    } },
  })
  const output = Array.isArray(result) ? result.flatMap(item => item.output) : result.output
  assets = {
    entry: output.find(item => item.type === 'chunk' && item.isEntry).fileName,
    files: new Map(output.map(item => ['/' + item.fileName, item.type === 'chunk' ? item.code : item.source])),
    css: output.filter(item => item.type === 'asset' && item.fileName.endsWith('.css')).map(item => item.source).join('\n'),
  }
})

async function open(page, { touch = false } = {}) {
  if (touch) await page.addInitScript(() => {
    const original = window.matchMedia.bind(window)
    window.matchMedia = query => {
      const result = original(query)
      if (query.includes('pointer: coarse')) Object.defineProperty(result, 'matches', { value: true })
      return result
    }
  })
  await page.route('**/*', route => {
    const path = new URL(route.request().url()).pathname
    if (assets.files.has(path)) return route.fulfill({ contentType: path.endsWith('.css') ? 'text/css' : 'text/javascript', body: assets.files.get(path) })
    if (path === '/fixture.css') return route.fulfill({ contentType: 'text/css', body: assets.css })
    if (path !== '/') return route.abort()
    return route.fulfill({ contentType: 'text/html', body: `<!doctype html><html><head><style>
      :root { --text:#eee; --bg:#161616; --surface:#222; --muted:#aaa; --accent:#9274f5; --border:#444; --mono:monospace; --font:Arial; }
      body { margin:0; padding:16px; background:var(--bg); color:var(--text); font-family:var(--font); }
      * { box-sizing:border-box; }
      </style><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/${assets.entry}"></script></body></html>` })
  })
  await page.goto('http://composer-fixture.test/')
  const editor = page.getByRole('textbox', { name: 'Message Möbius…' })
  await expect(editor).toBeVisible()
  await editor.focus()
  return editor
}
const text = page => page.evaluate(() => window.composerFixture.text())
const select = (page, from, to = from) => page.evaluate(([a,b])=>window.composerFixture.select(a,b), [from,to])
const paste = (page, values) => page.evaluate(values => {
  const data = new DataTransfer()
  for (const [type, value] of Object.entries(values)) data.setData(type, value)
  document.querySelector('.cm-content').dispatchEvent(new ClipboardEvent('paste', { bubbles:true, cancelable:true, clipboardData:data }))
}, values)

test('selection keeps readable theme text instead of inheriting the shell accent foreground', async ({ page }) => {
  const editor = await open(page)
  // Reproduce the global shell highlight competing with composer styling.
  await page.addStyleTag({ content: '::selection { background: #efebfe; color: #6c54c1; }' })
  const source = 'Plain **bold** text\nsecond line'
  await page.keyboard.insertText(source)
  for (const [theme, foreground, surface, mix] of [
    ['dark', '#ececec', '#171717', '40%'],
    ['light', '#202126', '#ffffff', '24%'],
  ]) {
    await page.evaluate(({ theme, foreground, surface }) => {
      document.documentElement.dataset.theme = theme
      document.documentElement.style.setProperty('--text', foreground)
      document.documentElement.style.setProperty('--surface', surface)
    }, { theme, foreground, surface })
    await select(page, 0, source.length)
    const colors = await editor.evaluate((element, mix) => {
      const expected = document.createElement('span')
      expected.style.cssText = `color:var(--text);background:color-mix(in srgb,var(--accent) ${mix},var(--surface))`
      element.append(expected)
      const target = getComputedStyle(expected)
      const want = { color: target.color, background: target.backgroundColor }
      expected.remove()
      return { want, actual: [element, ...element.querySelectorAll('.cm-line, .cm-md-strong, .cm-md-syntax')].map(node => {
        const style = getComputedStyle(node, '::selection')
        return { color: style.color, background: style.backgroundColor }
      }) }
    }, mix)
    for (const actual of colors.actual) expect(actual).toEqual(colors.want)
    expect(await text(page)).toBe(source)
    expect(await page.evaluate(() => window.composerFixture.selection())).toEqual({ start: 0, end: source.length })
  }
})

test('renders inline while preserving exact source, source selection and copy', async ({ page }) => {
  const editor = await open(page)
  const source = '# Heading\n**bold** _italic_ `code` [link](https://example.com)\n\nnext'
  await page.keyboard.insertText(source)
  await expect(editor.locator('.cm-md-strong')).toHaveText('bold')
  await expect(editor.locator('.cm-md-link')).toHaveText('link')
  expect(await text(page)).toBe(source)
  expect(await composerValue(editor)).toBe(source)
  await setComposerSelection(editor, 1, 2)
  expect(await composerSelection(editor)).toEqual({ start: 1, end: 2 })
  await select(page, 13)
  await expect(editor.locator('.cm-md-syntax').first()).toHaveText('**')
  await select(page, 0, source.length)
  const copied = await editor.evaluate(element => {
    const data = new DataTransfer()
    element.dispatchEvent(new ClipboardEvent('copy', { bubbles:true, cancelable:true, clipboardData:data }))
    return data.getData('text/plain')
  })
  expect(copied).toBe(source)
})

test('formatting, converted Markdown paste, undo and redo retain their exact edits', async ({ page }) => {
  await open(page)
  await page.keyboard.insertText('hello')
  await select(page, 0, 5)
  await page.keyboard.press('Control+b')
  await expect.poll(() => text(page)).toBe('**hello**')
  expect(await page.evaluate(()=>window.composerFixture.selection())).toEqual({ start:2, end:7 })
  await page.keyboard.press('Control+z')
  await expect.poll(() => text(page)).toBe('hello')
  await page.keyboard.press('Control+Shift+Z')
  await expect.poll(() => text(page)).toBe('**hello**')
  await select(page, 0, 9)
  await paste(page, { 'text/plain':'rich paste', 'text/markdown':'**rich paste**' })
  await expect.poll(() => text(page)).toBe('**rich paste**')
  await page.keyboard.press('Control+z')
  await expect.poll(() => text(page)).toBe('**hello**')
  expect(await page.evaluate(()=>window.composerFixture.intents.includes('input'))).toBe(true)
})

test('link paste wraps selected text, plain paste is literal, caret taps are respected', async ({ page }) => {
  const editor = await open(page)
  await page.keyboard.insertText('Review lesssons carefully')
  await select(page, 7, 15)
  await editor.evaluate(e => { e.blur(); e.focus() })
  expect(await page.evaluate(()=>window.composerFixture.selection())).toEqual({ start:7, end:15 })
  await page.keyboard.insertText('lessons')
  await expect.poll(() => text(page)).toBe('Review lessons carefully')
  await select(page, 7, 14)
  await paste(page, {'text/plain':'https://example.com'})
  await expect.poll(() => text(page)).toBe('Review [lessons](https://example.com) carefully')
  await page.keyboard.press('Control+z')
  await expect.poll(() => text(page)).toBe('Review lessons carefully')
  await select(page, 7, 14)
  await page.keyboard.down('Control')
  await page.keyboard.down('Shift')
  await page.keyboard.down('V')
  await paste(page, {'text/plain':'https://example.com'})
  await page.keyboard.up('V')
  await page.keyboard.up('Shift')
  await page.keyboard.up('Control')
  await expect.poll(() => text(page)).toBe('Review https://example.com carefully')
})

test('Enter sends exact Markdown, Shift+Enter inserts a line, sent text cannot return through undo', async ({ page }) => {
  await open(page)
  await page.keyboard.insertText('**message**')
  await page.keyboard.press('Shift+Enter')
  await page.keyboard.insertText('second line')
  await expect.poll(() => text(page)).toBe('**message**\nsecond line')
  await page.keyboard.press('Enter')
  await expect.poll(() => text(page)).toBe('')
  expect(await page.evaluate(()=>window.composerFixture.submissions)).toEqual(['**message**\nsecond line'])
  await page.keyboard.press('Control+z')
  expect(await text(page)).toBe('')
})

test('composition confirmation cannot send a desktop draft', async ({ page }) => {
  const editor = await open(page)
  await page.keyboard.insertText('draft')
  await editor.dispatchEvent('compositionstart', { data: '' })
  await page.keyboard.insertText('漢')
  await editor.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 229, isComposing: true })
  expect(await page.evaluate(() => window.composerFixture.submissions)).toEqual([])
  await editor.dispatchEvent('compositionend', { data: '漢' })
  await expect.poll(() => text(page)).toBe('draft漢')
})

test('touch Enter and composition do not submit; mobile spellcheck remains disabled', async ({ page }) => {
  const editor = await open(page, { touch:true })
  await expect(editor).toHaveAttribute('spellcheck','false')
  await page.keyboard.insertText('**mobile**')
  await page.keyboard.press('Enter')
  await expect.poll(() => text(page)).toBe('**mobile**\n')
  await editor.dispatchEvent('keydown', { key:'Enter', code:'Enter', isComposing:true })
  expect(await page.evaluate(()=>window.composerFixture.submissions)).toEqual([])
})

test('history at the visual edge restores the draft; in-text arrows move the caret', async ({ page }) => {
  await open(page)
  await page.keyboard.insertText('first\nsecond\nthird')
  await select(page, 10)
  await page.keyboard.press('ArrowUp')
  expect(await text(page)).toBe('first\nsecond\nthird')
  await select(page, 3)
  await page.keyboard.press('ArrowUp')
  await expect.poll(() => text(page)).toBe('older **message**')
  await page.keyboard.press('Escape')
  await expect.poll(() => text(page)).toBe('first\nsecond\nthird')
})

test('long input scrolls inside the capped composer and a controlled clear collapses it', async ({ page }) => {
  await open(page)
  await page.keyboard.insertText('a long line of text '.repeat(500))
  const geometry = await page.locator('.chat__markdown-input').evaluate(e=>({ height:e.getBoundingClientRect().height, max:parseFloat(getComputedStyle(e).maxHeight), scroller:e.querySelector('.cm-scroller').scrollHeight, client:e.querySelector('.cm-scroller').clientHeight }))
  expect(geometry.height).toBeLessThanOrEqual(geometry.max+1)
  expect(geometry.scroller).toBeGreaterThan(geometry.client)
  await page.evaluate(()=>window.composerFixture.replace(''))
  await expect.poll(async()=>page.locator('.chat__markdown-input').evaluate(e=>e.getBoundingClientRect().height)).toBeLessThan(50)
})


test('foreground reconciliation collapses stale empty-editor geometry', async ({ page }) => {
  await open(page)
  const host = page.locator('.chat__markdown-input')
  await host.evaluate(element => {
    element.style.height = '280px'
    element.closest('.chat__pill').setAttribute('data-composer-tall', '')
  })
  await expect(host).toHaveCSS('height', '280px')
  await page.evaluate(() => window.composerFixture.reconcile())
  await expect.poll(() => host.evaluate(element => ({
    collapsed: element.getBoundingClientRect().height < 60,
    tall: element.closest('.chat__pill').hasAttribute('data-composer-tall'),
  }))).toEqual({ collapsed: true, tall: false })
})
