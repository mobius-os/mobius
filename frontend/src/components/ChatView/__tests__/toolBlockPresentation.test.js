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

test('technical command failures stay behind the top-level disclosure', () => {
  assert.doesNotMatch(activityHeader, /exitCode|chat__activity-chip|displayState === 'error'/,
    'a collapsed activity overview must not present a command exit as a turn-level alarm')
  assert.match(toolBlock, /\{failed && !compact && \(/,
    'only a child already revealed by an expanded activity may show the header exit chip')
  assert.match(toolBlock,
    /r\.exitCode != null && r\.exitCode !== 0[\s\S]*className="chat__tool-exit"/,
    'the exact code remains available in the disclosed command output')
  assert.doesNotMatch(chatCss, /\.chat__activity--error|\.chat__activity-chip/,
    'collapsed activity chrome stays visually neutral')
})

test('viewed images expand directly without repeating their path or result card', () => {
  assert.match(toolBlock, /open && t\.input && !isImageTool/,
    'the disclosure row already names a viewed image path')
  assert.match(toolBlock, /isImageTool \? 'chat__tool-image-result' : 'chat__tool-section'/)
  assert.match(toolBlock, /isImageTool \? ' chat__tool--image' : ''/)
  assert.doesNotMatch(toolBlock, /isImageTool \? 'Image'/,
    'a bare image needs no redundant section label')
  assert.match(chatCss,
    /\.chat__tool--image\.chat__tool--compact \.chat__tool-detail[\s\S]*?border:\s*0;[\s\S]*?background:\s*none;/,
    'the expanded image does not regain the generic nested detail card')
})

test('a lone tool activity uses the borderless compact disclosure surface', () => {
  assert.match(toolBlock, /compact = false/,
    'ToolBlock exposes an explicit compact surface instead of styling every tool globally')
  assert.match(toolBlock, /chat__tool--compact/)
  assert.match(chatCss,
    /\.chat__tool--compact\.chat__tool--done\s*\{[^}]*background:\s*none;[^}]*border:\s*0;/s)
  assert.match(chatCss,
    /\.chat__tool--compact \.chat__tool-detail,\s*\.chat__activity-timeline \.chat__tool-detail\s*\{[^}]*border:\s*1px solid var\(--border-light\);[^}]*background:\s*var\(--surface\);/s,
    'expanding the quiet row reveals a nested output panel rather than restoring an outer card')
})

test('lone and grouped tools share the same disclosed detail boundary', () => {
  assert.match(chatCss,
    /\.chat__tool--compact \.chat__tool-detail,\s*\.chat__activity-timeline \.chat__tool-detail\s*\{[^}]*border:\s*1px solid var\(--border-light\);[^}]*border-radius:\s*10px;[^}]*background:\s*var\(--surface\);/s,
    'a completed grouped command should not lose the panel used by a lone live command')
})
