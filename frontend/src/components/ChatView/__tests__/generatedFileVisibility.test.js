import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const vite = await createServer({
  appType: 'custom',
  logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
const { default: MsgContent } = await vite.ssrLoadModule(
  '/src/components/ChatView/MsgContent.jsx',
)

after(() => vite.close())

const generatedMessage = {
  role: 'assistant',
  content: '',
  blocks: [{
    type: 'tool',
    tool: 'Bash',
    tool_use_id: 'tool-pdf',
    status: 'done',
    generated_files: [{
      name: 'report.pdf',
      size: 700,
      mime_type: 'application/pdf',
    }],
  }],
}

function renderGeneratedMessage(isStreaming) {
  return renderToStaticMarkup(createElement(MsgContent, {
    msg: generatedMessage,
    chatId: 'chat-generated-file',
    isLastMsg: true,
    isStreaming,
  }))
}

test('generated-file card stays hidden while the assistant is writing', () => {
  const html = renderGeneratedMessage(true)

  assert.doesNotMatch(html, /chat__generated-file-link/)
  assert.doesNotMatch(html, />report\.pdf</)
})

test('generated-file card appears after the assistant response settles', () => {
  const html = renderGeneratedMessage(false)

  assert.match(html, /chat__generated-file-link/)
  assert.match(html, />report\.pdf</)
})
