import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const shell = readFileSync(
  new URL('../Shell.jsx', import.meta.url),
  'utf8',
)

test('a committed summary rename refreshes live tab and drawer names', () => {
  assert.match(shell, /ev\.type === 'chat_renamed'/)
  assert.match(
    shell,
    /ev\.type === 'chat_renamed'[\s\S]*?invalidateShellListCache\('chats'\)\.then\(refreshChats\)/,
  )
})
