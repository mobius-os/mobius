/* A collapsed activity line names the app behind an app activity. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'

const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false } })
after(() => vite.close())
const { toolGroupSummary, toolGroupPastSummary } = await vite.ssrLoadModule('/src/components/ChatView/groupBlocks.js')

const memory = status => ({
  type: 'tool', tool: 'Bash', status: status === 'running' ? 'running' : 'done',
  app_activity: { status, app_slug: 'memory', app_name: 'Memory', label: 'Searching' },
})

test('a collapsed header names the app behind an app activity', () => {
  assert.equal(toolGroupSummary([memory('running')]), 'Using Memory')
  assert.equal(
    toolGroupPastSummary([{ type: 'tool', tool: 'Read', status: 'done' }, memory('succeeded')]),
    'Read a file, used Memory',
  )
})

test('an activity without a known app still reads as using an app', () => {
  const unnamed = { type: 'tool', tool: 'Bash', status: 'running', app_activity: { status: 'bogus' } }
  assert.equal(toolGroupSummary([unnamed]), 'Using an app')
})
