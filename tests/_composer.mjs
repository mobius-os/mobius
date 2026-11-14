/** Browser-test access to source text and selection without assuming a textarea or private CodeMirror internals. */
import { expect } from '@playwright/test'
import { rolldown } from '../frontend/node_modules/rolldown/dist/index.mjs'
import { fileURLToPath } from 'node:url'
let bundle
const pages = new WeakSet()
async function probeScript() {
  if (!bundle) bundle = (async () => {
    const build = await rolldown({ input: fileURLToPath(new URL('../frontend/src/components/ChatView/__tests__/textEditor.browser.js', import.meta.url)) })
    try {
      const result = await build.generate({ format: 'es' })
      const chunks = result.output.filter(item => item.type === 'chunk')
      if (chunks.length !== 1 || chunks[0].imports.length || chunks[0].exports.length) throw new Error('Composer probe must be one self-contained browser script')
      return `(() => { ${chunks[0].code}\n})()`
    } finally { await build.close() }
  })()
  return bundle
}
export async function installComposerProbe(page) {
  const script = await probeScript()
  if (!pages.has(page)) { await page.addInitScript({ content: script }); pages.add(page) }
  if (!await page.evaluate(() => !!globalThis.__mobiusComposerTest)) await page.evaluate(script)
}
export async function composerValue(locator) {
  await installComposerProbe(locator.page())
  return locator.evaluate(element => globalThis.__mobiusComposerTest.text(element))
}
export async function composerSelection(locator) {
  await installComposerProbe(locator.page())
  return locator.evaluate(element => globalThis.__mobiusComposerTest.selection(element))
}
export async function setComposerSelection(locator, start, end = start) {
  await installComposerProbe(locator.page())
  return locator.evaluate((element, [from,to]) => globalThis.__mobiusComposerTest.select(element,from,to), [start,end])
}
export function expectComposerValue(locator, value, options) {
  const assertion = expect.poll(() => composerValue(locator), options)
  return value instanceof RegExp ? assertion.toMatch(value) : assertion.toBe(value)
}
