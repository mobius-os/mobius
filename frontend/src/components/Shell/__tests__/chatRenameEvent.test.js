import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const shell = readFileSync(
  new URL('../Shell.jsx', import.meta.url),
  'utf8',
)

test('a committed summary rename projects exact fields without a list refetch', () => {
  assert.match(shell, /ev\.type === 'chat_renamed'/)
  assert.match(
    shell,
    /ev\.type === 'chat_renamed'[\s\S]*?applyChatRenameEvent\(ev\)[\s\S]*?invalidateShellListCache\('chats'\)/,
  )
  const renamePath = shell.slice(
    shell.indexOf("ev.type === 'chat_renamed'"),
    shell.indexOf("ev.type === 'app_deleted'"),
  )
  assert.doesNotMatch(renamePath, /refreshChats/)
  assert.match(shell, /chatRenameGuardsRef\.current\.set\(String\(event\.chatId\)/)
  assert.match(shell, /reconcileChatRenameGuards\([\s\S]*?chatRenameGuardsRef\.current/)
  assert.match(shell, /title: event\.title[\s\S]*?updatedAt: event\.updatedAt/)
})
