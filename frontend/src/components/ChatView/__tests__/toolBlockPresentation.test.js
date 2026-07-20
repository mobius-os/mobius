import { readFileSync } from 'node:fs'
import test from 'node:test'
import assert from 'node:assert/strict'

const toolBlock = readFileSync(new URL('../ToolBlock.jsx', import.meta.url), 'utf8')
const activityHeader = readFileSync(new URL('../ActivityLineHeader.jsx', import.meta.url), 'utf8')
const chatCss = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')

test('activity and child tool disclosures use leading fixed lanes', () => {
  const activityDisclosure = activityHeader.indexOf('className={\n          `chat__activity-disclosure')
  const activityIcon = activityHeader.indexOf('className="chat__activity-icon"')
  assert.ok(activityDisclosure >= 0 && activityDisclosure < activityIcon,
    'the parent disclosure belongs before its type icon')

  const toolDisclosure = toolBlock.indexOf('`chat__tool-toggle')
  const toolIcon = toolBlock.indexOf('`chat__tool-icon')
  const toolName = toolBlock.indexOf('className="chat__tool-name"')
  assert.ok(toolDisclosure >= 0 && toolDisclosure < toolIcon && toolIcon < toolName,
    'child order is disclosure, type icon, then label')
  assert.doesNotMatch(toolBlock, /\{open \? '▾' : '▸'\}/,
    'the old trailing text chevron is gone')
  assert.match(toolBlock, /aria-expanded=\{open\}/,
    'the real button communicates disclosure state')
  assert.match(toolBlock, /chat__tool-toggle--spacer/,
    'static rows reserve the disclosure lane instead of shifting')
})

test('tool detail is a third nested level with labeled command and output', () => {
  assert.match(toolBlock, /\{isShell \? 'Command' : 'Input'\}/)
  assert.match(toolBlock, /\{isShell \? 'Output' : 'Result'\}/)
  assert.match(toolBlock, /className="chat__tool-prompt" aria-hidden="true">\$ <\/span>/,
    'shell input gets a decorative prompt without polluting the accessible text')
  assert.match(toolBlock, /formatToolResult\(shownOutput \?\? '', \{ terminal: isShell \}\)/,
    'plain command output receives terminal-aware formatting')
  assert.match(toolBlock, /t\.status !== 'running' && shownOutput === ''/,
    'only a settled empty command reports No output')
  assert.match(chatCss, /\.chat__activity-timeline \.chat__tool-detail\s*\{[^}]*margin-inline-start:\s*40px/s,
    'output aligns beneath the child label, not beneath its disclosure')
})
