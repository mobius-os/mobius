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

  const write = /style\.setProperty\(\s*['"]--composer-h['"]/
  const clearanceWriters = sourceFiles(chatViewDir).filter(({ full }) => (
    write.test(readFileSync(full, 'utf8'))
  )).map(f => f.name).sort()
  assert.deepEqual(clearanceWriters, [OWNER],
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
  assert.match(
    hotPath,
    /clearTimeout\(readerSettleTimer\)[\s\S]*?setTimeout\(settleReaderScroll, GESTURE_SETTLE_MS\)/,
    'every browser should have one guaranteed trailing-edge settlement path',
  )
  assert.doesNotMatch(hotPath, /hasNativeScrollEnd/,
    'feature detection must not trust browsers to deliver an advertised scrollend')
  assert.match(
    ownerSource,
    /addEventListener\('scrollend', settleReaderScroll/,
    'native scrollend should complete the same settlement path early',
  )
  assert.match(hotPath, /atBottom:\s*distanceToBottom\s*<=\s*FOLLOW_STICK_BAND_PX/,
    'the intent reducer must latch the follow-stick band, not a pixel-exact tail')
  assert.match(hotPath, /readerScrollEscapeDirection\(/,
    'directionless native scrollbar movement must update the escape latch')

  const settleStart = ownerSource.indexOf('const settleReaderScroll = () => {')
  const settleEnd = ownerSource.indexOf(
    'const releasePendingGesture = (sequence) => {',
    settleStart,
  )
  const settlePath = ownerSource.slice(settleStart, settleEnd)
  assert.ok(settleStart >= 0 && settleEnd > settleStart,
    'reader settlement path must remain discoverable')
  assert.match(settlePath, /anchorModeFromScroll/)
  assert.match(settlePath, /modeAfterReaderGesture/)
  assert.match(settlePath, /escaped:\s*settledEscaped/)
  assert.match(settlePath, /reachedNearBottom:\s*settledReachedNearBottom/)
  assert.match(settlePath, /wasFollowing/)
  assert.doesNotMatch(settlePath, /spacerH|hasReservedTail/,
    'physical-bottom intent must not branch on invisible reservation')
  assert.match(settlePath, /persistMode\(\)/)
  assert.match(
    settlePath,
    /syncLayout\(\{ forceApply: true, authorityVersion: currentAuthority\(\) \}\)/,
    'settlement must resize and re-apply the captured semantic coordinate atomically',
  )
  assert.doesNotMatch(settlePath, /PIN_USER_MSG|contentHoldModeFromScroll/,
    'a reader gesture may hold or follow but must never recreate pin authority')
  assert.doesNotMatch(
    settlePath,
    /scrollEl\.scrollHeight\s*>\s*scrollEl\.clientHeight/,
    'a live spacer collapse must not leave the pre-gesture pin armed',
  )
})

test('automatic geometry owners and newer semantic actions share reader authority', () => {
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

  const readerHotStart = ownerSource.indexOf('const onScroll = () => {')
  const readerHotEnd = ownerSource.indexOf(
    "scrollEl.addEventListener('scroll', onScroll",
    readerHotStart,
  )
  const readerHotPath = ownerSource.slice(readerHotStart, readerHotEnd)
  assert.match(
    readerHotPath,
    /readerIntentAfterScroll\(\{/,
    'actual scrolls must claim generations by input sequence, not quiet batch',
  )

  assert.match(terminalPath, /authority === 'wait'/)
  assert.match(terminalPath, /requestAnimationFrame\(inspectCommittedLayout\)/,
    'terminal settlement must wait through a no-scroll tap instead of retiring pin')
  assert.doesNotMatch(terminalPath, /terminal:reader-owns/)

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
  assert.match(hotPath, /if \(gesture\.disclosureOwns\) return/,
    'layout scrolls caused by a disclosure must not create a stale reader settle')
  assert.match(
    ownerSource,
    /const onPointerCancelInput = \(\) => \{[\s\S]*?gesture\.disclosureOwns = false[\s\S]*?addEventListener\('pointercancel', onPointerCancelInput/,
    'a disclosure press promoted to a native pan must become reader-owned scroll',
  )

  const composerStart = ownerSource.indexOf('const runComposerTailIntent =')
  const composerEnd = ownerSource.indexOf('const noteScrollStart =', composerStart)
  const composerPath = ownerSource.slice(composerStart, composerEnd)
  assert.ok(composerStart >= 0 && composerEnd > composerStart,
    'composer tail intent must remain inside the scroll owner')
  assert.match(composerPath, /composerTailIntentRequestsFollow\(event, scrollEl\)/,
    'composer focus/edit may follow only after checking pre-resize tail geometry')
  assert.ok(
    composerPath.indexOf('supersedePendingReaderGesture()')
      < composerPath.indexOf("transitionMode({ kind: 'FOLLOW_BOTTOM' }"),
    'composer intent must retire an older gesture before keyboard follow begins',
  )
})
