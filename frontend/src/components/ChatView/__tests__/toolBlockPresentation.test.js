import { readFileSync } from 'node:fs'
import test from 'node:test'
import assert from 'node:assert/strict'

const toolBlock = readFileSync(new URL('../ToolBlock.jsx', import.meta.url), 'utf8')
const activityHeader = readFileSync(new URL('../ActivityLineHeader.jsx', import.meta.url), 'utf8')
const chatCss = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')

test('activity and child tool disclosures use icons without chevrons', () => {
  const activityIcon = activityHeader.indexOf('className="chat__activity-icon"')
  assert.ok(activityIcon >= 0, 'the parent keeps its type icon')

  const toolIcon = toolBlock.indexOf('`chat__tool-icon')
  const toolName = toolBlock.indexOf('className="chat__tool-name"')
  assert.ok(toolIcon >= 0 && toolIcon < toolName,
    'child order is type icon, then label')
  assert.doesNotMatch(activityHeader + toolBlock,
    /chat__(?:activity-disclosure|tool-toggle)/,
    'parent and tool rows should not render disclosure chevrons')
  assert.match(toolBlock, /aria-expanded=\{open\}/,
    'the real button communicates disclosure state')
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
  assert.match(chatCss, /\.chat__activity-timeline \.chat__tool-detail\s*\{[^}]*margin-inline-start:\s*20px/s,
    'output aligns beneath the child label')
})

test('a lone tool activity uses the borderless compact disclosure surface', () => {
  assert.match(toolBlock, /compact = false/,
    'ToolBlock exposes an explicit compact surface instead of styling every tool globally')
  assert.match(toolBlock, /chat__tool--compact/)
  assert.match(chatCss,
    /\.chat__tool--compact\.chat__tool--done\s*\{[^}]*background:\s*none;[^}]*border:\s*0;/s)
  assert.match(chatCss,
    /\.chat__tool--compact \.chat__tool-detail\s*\{[^}]*border:\s*1px solid var\(--border-light\);[^}]*background:\s*var\(--surface\);/s,
    'expanding the quiet row reveals a nested output panel rather than restoring an outer card')
})
