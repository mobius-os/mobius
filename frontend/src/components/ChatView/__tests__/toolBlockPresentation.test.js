import { readFileSync } from 'node:fs'
import test from 'node:test'
import assert from 'node:assert/strict'

const toolBlock = readFileSync(new URL('../ToolBlock.jsx', import.meta.url), 'utf8')
const toolImageResult = readFileSync(new URL('../ToolImageResult.jsx', import.meta.url), 'utf8')
const toolImagePreview = readFileSync(new URL('../useToolImagePreview.js', import.meta.url), 'utf8')
const toolEditPreviewCss = readFileSync(new URL('../ToolEditPreview.css', import.meta.url), 'utf8')
const activityHeader = readFileSync(new URL('../ActivityLineHeader.jsx', import.meta.url), 'utf8')
const chatCss = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
const indexCss = readFileSync(new URL('../../../index.css', import.meta.url), 'utf8')

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

test('tool call labels participate in native transcript text selection', () => {
  const headerRule = chatCss.match(
    /(?:^|\n)\.chat__tool-header\s*\{[^}]*\}/s,
  )?.[0] || ''
  assert.match(headerRule, /user-select:\s*text/,
    'desktop drag selection should include the visible tool call')
  assert.match(headerRule, /-webkit-user-select:\s*text/,
    'WebKit should not inherit the global button selection lock')
  assert.match(headerRule, /-webkit-touch-callout:\s*default/,
    'touch selection keeps the native action menu')
  assert.doesNotMatch(
    indexCss,
    /\.chat__tool-header\s*,\s*\.tool-block__header\s*\{[^}]*user-select:\s*none/s,
    'the global chrome lock must not compete with the current disclosure owner',
  )
  assert.match(
    toolBlock,
    /onPointerDown=\{\(\) => \{[\s\S]*pointerSelectionRef\.current = textSelectionSnapshot\(\)[\s\S]*pointerSelectionChangedWithin\([\s\S]*headerRef\.current[\s\S]*setPrepareRequested\(false\)[\s\S]*releaseClosedDetail\(\)[\s\S]*return[\s\S]*const nextOpen/,
    'releasing a pointer selection must cancel preparation without toggling',
  )
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

test('edit detail hands vertical scrolling to the transcript', () => {
  assert.match(toolBlock,
    /editPreview \? ' chat__tool-detail--edit' : ''/,
    'only a successfully parsed edit preview escapes the generic output cap')
  const editRule = toolEditPreviewCss.match(
    /\.chat__tool-detail\.chat__tool-detail--edit\s*\{[^}]*\}/s,
  )?.[0] || ''
  assert.match(editRule, /max-height:\s*none/)
  assert.match(editRule, /overflow-y:\s*visible/,
    'an edit card must not retain a second vertical scroll owner')
  assert.doesNotMatch(editRule, /overflow-y:\s*(?:auto|scroll)/)
})

test('successful edit disclosures are the file names and diffs, without duplicate IO', () => {
  assert.match(toolBlock,
    /const showGenericInput = !!\(open && t\.input && !isImageTool && !editPreview\)/,
    'the diff file header already owns every edited path')
  assert.match(toolBlock,
    /open && !editPreview && \(r \|\| t\.output_truncated \|\| isImageTool\)/,
    'provider success output is hidden only when a usable edit preview replaces it')
  assert.match(toolBlock, /\{showGenericInput && \(/)
  assert.match(toolBlock, /\{showGenericResult && \(/)
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

test('failed edit tools do not present proposed input as completed changes', () => {
  assert.match(toolBlock, /const failed = toolBlockFailed\(t\)/)
  assert.match(
    toolBlock,
    /wantsPreparation && !failed \? toolEditPreview\(t\.edit_preview\) : null/,
  )
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

test('cold tool and image detail reveal only after their final layout is ready', () => {
  assert.match(
    toolBlock,
    /const detailReady = previewReady && imageReady\s*const open = desiredOpen && detailReady/,
    'user intent is distinct from the one rendered open boundary',
  )
  assert.match(
    toolBlock,
    /onPointerDown=\{\(\) => \{[\s\S]*textSelectionSnapshot\(\)[\s\S]*setPrepareRequested\(true\)\s*\}/,
    'pointer-down starts preparation before the click boundary')
  assert.match(
    toolBlock,
    /onKeyDown=\{\(event\) => \{[\s\S]*event\.key === 'Enter' \|\| event\.key === ' '[\s\S]*setPrepareRequested\(true\)/,
    'keyboard activation gets the same first-open preparation without prefetching every tab stop',
  )
  assert.match(
    toolBlock,
    /if \(!isImageTool\) revealBeforeReady\(\)\s*setPreviewOutput\(text\)/,
    'bounded text output preserves position immediately before it becomes revealable',
  )
  assert.match(toolImagePreview, /const image = new window\.Image\(\)/,
    'viewed images decode through a detached browser image')
  assert.match(toolImagePreview, /await image\.decode\(\)/)
  assert.match(
    toolImagePreview,
    /onSettledRef\.current\?\.\(\)\s*setPreview\(\{\s*reference,\s*status: 'ready'/,
    'the disclosure boundary is armed before decoded dimensions enter state',
  )
  assert.match(toolImageResult, /preview\?\.reference === reference/)
  assert.match(toolImageResult, /imageLoading="eager"/,
    'the already-decoded resource is painted immediately when inserted')
  assert.doesNotMatch(toolImageResult, /aria-busy|Loading image|useEffect/,
    'the visible image body has no transient loading layout')
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
