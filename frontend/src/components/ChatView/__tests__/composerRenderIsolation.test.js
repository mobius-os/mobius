import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'


const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const composerOwner = readFileSync(
  new URL('../hooks/useComposerDraftState.js', import.meta.url),
  'utf8',
)
const activeSurface = readFileSync(
  new URL('../ActiveAssistantSurface.jsx', import.meta.url),
  'utf8',
)

test('composer edits cannot recreate the active assistant payload', () => {
  assert.match(activeSurface, /export default memo\(ActiveAssistantSurface\)/)
  assert.match(activeSurface, /const msg = useMemo\(/)
  assert.match(activeSurface, /streamItemsToAssistantPayload\(streamItems/)
  assert.match(chatView, /<ActiveAssistantSurface/)
  assert.match(
    chatView,
    /useMemo\(\(\) => deriveActiveAssistantSelection\(/,
  )
  assert.doesNotMatch(chatView, /const activeAssistantMsg\s*=/)
})

test('draft persistence has one state-boundary owner', () => {
  const setter = composerOwner.match(
    /const setComposerInput = useCallback\(\(nextInput\) => \{[\s\S]*?\n  \}, \[chatId\]\)/,
  )?.[0] || ''
  assert.match(setter, /persistComposerDraft\(/)
  assert.match(setter, /setInputState\(/)
  assert.doesNotMatch(
    composerOwner,
    /useEffect\(\(\) => \{\s*persistComposerDraft\(chatId, input,/,
  )
})
