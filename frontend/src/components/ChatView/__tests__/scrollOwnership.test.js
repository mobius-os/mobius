import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'

// Sole-writer guard for the dynamic bottom spacer. useScrollMode derives its
// exact height from the latest user row's visibility and content deficit. A
// disclosure, renderer, or component writing the same height independently can
// strand provisional blank room after QA/tool/image layout changes, so any
// second writer is a contract bug.

const dir = dirname(fileURLToPath(import.meta.url))
const chatViewDir = join(dir, '..')

const OWNER = 'useScrollMode.js'
const ownerSource = readFileSync(join(chatViewDir, OWNER), 'utf8')

// A line that assigns a height to the dynamic spacer. Matches
// `spacer.style.height = ...` / `spacerEl.style.height = ...` on any variable,
// scoped to files that reference the spacer selector so a stray `.style.height`
// on an unrelated element (e.g. the composer textarea) is not a false hit.
const SPACER_HEIGHT_WRITE = /\.style\.height\s*=/

function sourceFiles(root) {
  const out = []
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    if (entry.name === '__tests__' || entry.name === 'node_modules') continue
    const full = join(root, entry.name)
    if (entry.isDirectory()) out.push(...sourceFiles(full))
    else if (/\.(js|jsx)$/.test(entry.name)) out.push({ name: entry.name, full })
  }
  return out
}

test('only sanctioned modules write the dynamic spacer height', () => {
  const offenders = []
  for (const { name, full } of sourceFiles(chatViewDir)) {
    if (name === OWNER) continue
    const src = readFileSync(full, 'utf8')
    if (!src.includes('.spacer-dynamic')) continue
    if (SPACER_HEIGHT_WRITE.test(src)) offenders.push(name)
  }
  assert.deepEqual(offenders, [],
    `these modules write .spacer-dynamic height outside its sole owner `
    + `(${OWNER}): ${offenders.join(', ')}. Route spacer sizing `
    + `through useScrollMode's sizeSpacer instead of mutating it directly.`)
})

test('the sole spacer owner still exists (guard is not vacuous)', () => {
  // If a refactor renames these files the guard above would silently pass with
  // nothing to check; assert the owners are present so the guard stays live.
  const writers = sourceFiles(chatViewDir).filter(({ full }) => {
    const src = readFileSync(full, 'utf8')
    return src.includes('.spacer-dynamic') && SPACER_HEIGHT_WRITE.test(src)
  }).map(f => f.name).sort()
  assert.deepEqual(writers, [OWNER])
})

test('only the scroll owner publishes composer clearance geometry', () => {
  const write = /style\.setProperty\(\s*['"]--composer-h['"]/
  const writers = sourceFiles(chatViewDir).filter(({ full }) => (
    write.test(readFileSync(full, 'utf8'))
  )).map(f => f.name).sort()
  assert.deepEqual(writers, [OWNER],
    'composer clearance can clamp scrollTop indirectly, so route it through '
    + 'the same reader-authority gate as spacer and direct scroll writes')
})

test('gesture scroll frames defer anchor, spacer, and persistence work until settle', () => {
  const start = ownerSource.indexOf('const onScroll = () => {')
  const end = ownerSource.indexOf(
    "scrollEl.addEventListener('scroll', onScroll",
    start,
  )
  const hotPath = ownerSource.slice(start, end)
  assert.ok(start >= 0 && end > start, 'scroll hot path must remain discoverable')
  assert.doesNotMatch(
    hotPath,
    /persistMode|sizeSpacer|contentHoldModeFromScroll|_lastUserRowEl|querySelector/,
    'per-frame scroll handling must not traverse messages, resize layout, or persist',
  )
  assert.match(hotPath, /scheduleReaderSettle\(\)/,
    'scroll frames should hand final semantic work to one trailing-edge settle')
  assert.match(
    hotPath,
    /readerScrollAtBottom\s*=\s*distanceToBottom\s*<\s*PHYSICAL_BOTTOM_EPSILON_PX/,
    'the event must preserve explicit tail intent before live output can move it',
  )

  const settleStart = ownerSource.indexOf('const settleReaderScroll = () => {')
  const settleEnd = ownerSource.indexOf(
    'const scheduleReaderSettle = () => {',
    settleStart,
  )
  const settlePath = ownerSource.slice(settleStart, settleEnd)
  assert.ok(settleStart >= 0 && settleEnd > settleStart,
    'reader settlement path must remain discoverable')
  assert.match(settlePath, /anchorModeFromScroll/)
  assert.match(settlePath, /modeAfterReaderGesture/)
  assert.match(settlePath, /hasReservedTail:\s*spacerH\s*>\s*1/)
  assert.match(settlePath, /persistMode\(\)/)
  assert.match(settlePath, /sizeSpacer\(currentAuthority\(\)\)/)
  assert.doesNotMatch(settlePath, /PIN_USER_MSG|contentHoldModeFromScroll/,
    'a reader gesture may hold or follow but must never recreate pin authority')
  assert.doesNotMatch(
    settlePath,
    /scrollEl\.scrollHeight\s*>\s*scrollEl\.clientHeight/,
    'a live spacer collapse must not leave the pre-gesture pin armed',
  )
})

test('every automatic geometry owner shares the reader-generation gate', () => {
  const writeStart = ownerSource.indexOf('const writeMode = useCallback(')
  const writeEnd = ownerSource.indexOf('const persistMode =', writeStart)
  const writePath = ownerSource.slice(writeStart, writeEnd)
  assert.match(writePath, /scrollAuthorityAllowsCommit/,
    'direct scroll writes must reject stale generations')

  const spacerStart = ownerSource.indexOf('function sizeSpacer(')
  const spacerEnd = ownerSource.indexOf('function maybeApplyMode(', spacerStart)
  const spacerPath = ownerSource.slice(spacerStart, spacerEnd)
  assert.match(spacerPath, /layoutOwnsScroll\(authorityVersion\)/)
  assert.ok(
    spacerPath.indexOf('layoutOwnsScroll(authorityVersion)')
      < spacerPath.indexOf("style.setProperty('--composer-h'"),
    'composer clearance must be gated before it mutates layout',
  )
  assert.ok(
    spacerPath.indexOf('layoutOwnsScroll(authorityVersion)')
      < spacerPath.indexOf('spacerEl.style.height ='),
    'spacer height must be gated before it mutates layout',
  )

  const terminalStart = ownerSource.indexOf('const settleStreamingPin =')
  const terminalEnd = ownerSource.indexOf('const paneResized =', terminalStart)
  const terminalPath = ownerSource.slice(terminalStart, terminalEnd)
  assert.match(terminalPath, /terminalAuthorityVersion/)
  assert.match(terminalPath, /scrollAuthorityAllowsCommit/,
    'terminal rAF work must reject a later reader generation')

  const hotStart = ownerSource.indexOf('const onScroll = () => {')
  const hotEnd = ownerSource.indexOf(
    "scrollEl.addEventListener('scroll', onScroll",
    hotStart,
  )
  const hotPath = ownerSource.slice(hotStart, hotEnd)
  assert.match(
    hotPath,
    /readerIntentAfterScroll\(\{/,
    'actual scrolls must claim generations by input sequence, not quiet batch',
  )

  assert.match(terminalPath, /authority === 'wait'/)
  assert.match(terminalPath, /requestAnimationFrame\(inspectCommittedLayout\)/,
    'terminal settlement must wait through a no-scroll tap instead of retiring pin')
  assert.doesNotMatch(terminalPath, /terminal:reader-owns/)
})

test('newer semantic actions cannot be overwritten by an older quiet settlement', () => {
  const supersedeStart = ownerSource.indexOf(
    'const supersedePendingReaderGesture =',
  )
  const supersedeEnd = ownerSource.indexOf(
    'const captureSendIntent =',
    supersedeStart,
  )
  const supersedePath = ownerSource.slice(supersedeStart, supersedeEnd)
  assert.ok(supersedeStart >= 0 && supersedeEnd > supersedeStart,
    'semantic-action ownership handoff must remain discoverable')
  assert.match(supersedePath, /discardPendingReaderSettleRef\.current\?\.\(\)/)

  const captureStart = supersedeEnd
  const captureEnd = ownerSource.indexOf(
    'const sendIntentIsCurrent =',
    captureStart,
  )
  const capturePath = ownerSource.slice(captureStart, captureEnd)
  assert.ok(
    capturePath.indexOf('shouldPinSend({')
      < capturePath.indexOf('supersedePendingReaderGesture()'),
    'send must snapshot current geometry before retiring the gesture that positioned it',
  )

  const questionStart = ownerSource.indexOf(
    'const freezeQuestionSubmission =',
  )
  const questionEnd = ownerSource.indexOf(
    'const anchorPagination =',
    questionStart,
  )
  const questionPath = ownerSource.slice(questionStart, questionEnd)
  assert.ok(
    questionPath.indexOf('modeForQuestionSubmission(')
      < questionPath.indexOf('supersedePendingReaderGesture()'),
    'question submit must snapshot its card before retiring older settlement',
  )
  assert.ok(
    questionPath.indexOf('supersedePendingReaderGesture()')
      < questionPath.indexOf('transitionMode('),
    'older settlement must be retired before the question anchor is committed',
  )

  const hotStart = ownerSource.indexOf('const onScroll = () => {')
  const hotEnd = ownerSource.indexOf(
    "scrollEl.addEventListener('scroll', onScroll",
    hotStart,
  )
  const hotPath = ownerSource.slice(hotStart, hotEnd)
  assert.match(hotPath, /if \(disclosureInputOwnsGesture\) return/,
    'layout scrolls caused by a disclosure must not create a stale reader settle')
})
