/**
 * Render the real inline/display math sinks and inspect the config they pass
 * to DOMPurify. The sanitizer double returns deliberately different HTML, so
 * bypassing it (or ignoring its return value) cannot pass. This is wiring and
 * policy coverage, NOT DOMPurify's browser parsing/XSS behavior.
 *
 * cd frontend && node --loader=./src/lib/__tests__/vite-env-loader.mjs \
 *   --test src/components/ChatView/__tests__/katexSanitize.test.js
 */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

const purifierId = '\0katex-purifier-spy'
const mathId = '\0katex-html-fixture'
const vite = await createSsrRenderServer({
  configFile: false,
  noExternal: ['dompurify'],
  plugins: [{
    name: 'katex-sanitize-boundaries',
    enforce: 'pre',
    resolveId(id, importer) {
      if (id === 'dompurify') return purifierId
      if (id === './math.js' && /\/markdown\/(InlineContent|blocks)\.jsx$/.test(importer || '')) return mathId
    },
    load(id) {
      if (id === purifierId) return `
        export const calls = [];
        export default { sanitize(html, config) {
          const clean = '<math data-sanitized="' + (calls.length + 1) + '"><mi>safe</mi></math>';
          calls.push({ html, config, clean });
          return clean;
        } };
      `
      if (id === mathId) return `
        export const calls = [];
        export let output = '';
        export function setOutput(value) { output = value; }
        export function useMathHtml(tex, displayMode) {
          calls.push({ tex, displayMode });
          return output;
        }
      `
    },
  }],
})
after(() => vite.close())
const { default: InlineContent } = await vite.ssrLoadModule('/src/components/ChatView/markdown/InlineContent.jsx')
const { MathBlock } = await vite.ssrLoadModule('/src/components/ChatView/markdown/blocks.jsx')
const purifier = await vite.ssrLoadModule(purifierId)
const math = await vite.ssrLoadModule(mathId)

const sites = [
  { name: 'inline token', tag: 'span', className: 'md-math-inline', displayMode: false,
    element: tex => createElement(InlineContent, { tokens: [{ type: 'inlineKatex', text: tex, displayMode: false }] }) },
  { name: 'display-mode inline token', tag: 'div', className: 'md-math-block', displayMode: true,
    element: tex => createElement(InlineContent, { tokens: [{ type: 'inlineKatex', text: tex, displayMode: true }] }) },
  { name: 'block math', tag: 'div', className: 'md-math-block', displayMode: true,
    element: tex => createElement(MathBlock, { tex }) },
]

function assertMathPolicy(config) {
  assert.ok(config, 'the sink must supply its production MathML policy')
  for (const tag of ['math', 'mrow', 'mn', 'mo', 'mi', 'mfrac', 'msqrt', 'msup', 'msub']) {
    assert.ok(config.ADD_TAGS.includes(tag), `MathML <${tag}> must remain supported`)
  }
  for (const attr of ['display', 'xmlns']) {
    assert.ok(config.ADD_ATTR.includes(attr), `${attr} must remain supported`)
  }
  assert.equal(config.FORCE_BODY, true)
  assert.ok(!config.ADD_TAGS.includes('script'), 'script must not be added to the policy')
  assert.ok(!config.ADD_ATTR.some(attr => /^on/i.test(attr)), 'event-handler attrs must not be added')
}

for (const site of sites) {
  test(`${site.name} sends raw math through the actual sanitizer policy and renders only its result`, () => {
    purifier.calls.length = 0
    math.calls.length = 0
    const raw = '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math>'
      + '<script>unsafe()</script><img src=x onerror="unsafe()">'
    math.setOutput(raw)
    const html = renderToStaticMarkup(site.element('x + 1'))
    assert.equal(math.calls.length, 1)
    assert.deepEqual(math.calls[0], { tex: 'x + 1', displayMode: site.displayMode })
    assert.equal(purifier.calls.length, 1, 'every HTML sink must invoke DOMPurify exactly once')
    const call = purifier.calls[0]
    assert.equal(call.html, raw, 'raw useMathHtml output must reach the actual sanitizer')
    assertMathPolicy(call.config)
    assert.equal(html, `<${site.tag} class="${site.className}">${call.clean}</${site.tag}>`)
    assert.ok(!html.includes('unsafe'), 'raw HTML must never reach the sink')
  })

  test(`${site.name} escapes TeX while math HTML is unavailable`, () => {
    purifier.calls.length = 0
    math.setOutput(null)
    const html = renderToStaticMarkup(site.element('<img src=x onerror=unsafe()>'))
    assert.equal(purifier.calls.length, 0)
    assert.equal(html, `<${site.tag} class="${site.className}">&lt;img src=x onerror=unsafe()&gt;</${site.tag}>`)
  })
}
