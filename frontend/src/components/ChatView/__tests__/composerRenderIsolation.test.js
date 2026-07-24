import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'


const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
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
  const setter = chatView.match(
    /function setComposerInput\(nextInput\) \{[\s\S]*?\n  \}/,
  )?.[0] || ''
  assert.match(setter, /persistComposerDraft\(/)
  assert.match(setter, /setInputState\(/)
  assert.doesNotMatch(
    chatView,
    /useEffect\(\(\) => \{\s*persistComposerDraft\(chatId, input,/,
  )
})
