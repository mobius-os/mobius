// Render real disclosures: saved structured inputs and quiet receipts must remain usable.
import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

test('real expanded quiet tool rows render historical argument objects safely', async () => {
  const vite = await createServer({ appType: 'custom', logLevel: 'error',
    server: { middlewareMode: true, hmr: false, ws: false },
    ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
  try {
    const { default: ToolBlock } = await vite.ssrLoadModule('/src/components/ChatView/ToolBlock.jsx')
    const { default: ActivityStretch } = await vite.ssrLoadModule('/src/components/ChatView/ActivityStretch.jsx')
    const { persistDisclosureOpen } = await vite.ssrLoadModule('/src/components/ChatView/disclosureState.js')
    const rows = [
      { tool: 'checkpoint_chat', input: { title: 'Title', digest: 'Context', summary: 'Continue safely' } },
      { tool: 'reflection_log_friction', input: { friction: 'A synthetic diagnostic' } },
      { tool: 'memory_remember', input: { fact: 'A synthetic fact' } },
    ].map((row, index) => ({ ...row, type: 'tool', tool: `mcp__mobius_control__${row.tool}`,
      status: 'done', tool_use_id: `quiet-${index}`, delivery: 'quiet', output: 'Saved.', output_exit_code: 0 }))
    for (const row of rows) {
      const key = row.tool_use_id
      persistDisclosureOpen('quiet-render-test', key, true)
      const html = renderToStaticMarkup(React.createElement(ToolBlock, {
        t: row, chatId: 'quiet-render-test', disclosureKey: key,
      }))
      assert.ok(html.includes('chat__tool-section'))
      for (const value of Object.values(row.input)) assert.ok(html.includes(value))
      assert.equal(typeof row.input, 'object', 'rendering does not rewrite historical input')
    }
    // A lone save reads as plain note-keeping; its content stays behind expansion.
    const lone = renderToStaticMarkup(React.createElement(ActivityStretch, {
      entries: [{ item: rows[0], idx: 0 }], chatId: 'quiet-lone-test', surfaceKey: 'test',
    }))
    assert.ok(lone.includes('Saved notes'))
    assert.ok(!lone.includes('Saved chat notes'))
    assert.ok(!lone.includes('Continue safely'), 'receipt is available only after expansion')
    const capture = { ...rows[2], app_activity: {
      app_slug: 'memory', app_name: 'Memory', activity_id: 'memory-capture',
      status: 'succeeded', label: 'Saved to Memory', detail: 'A synthetic fact',
    } }
    const captureLone = renderToStaticMarkup(React.createElement(ActivityStretch, {
      entries: [{ item: capture, idx: 0 }], chatId: 'quiet-capture-test', surfaceKey: 'test',
    }))
    assert.ok(captureLone.includes('Saved notes'))
    assert.ok(!captureLone.includes('Saved to Memory'))
    assert.ok(!captureLone.includes('A synthetic fact'))
    persistDisclosureOpen('quiet-capture-expanded', 'capture', true)
    const captureExpanded = renderToStaticMarkup(React.createElement(ToolBlock, {
      t: capture, chatId: 'quiet-capture-expanded', compact: true, disclosureKey: 'capture',
    }))
    assert.ok(captureExpanded.includes('A synthetic fact'), 'the app-owned receipt survives quiet presentation')
  } finally {
    await vite.close()
  }
})
