/* Möbius control tools read as owner activities, never raw MCP identifiers,
   whichever provider reported them. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'

const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false } })
after(() => vite.close())
const { toolCallLabel, effectiveToolName, toolActivityIcon } = await vite.ssrLoadModule('/src/components/ChatView/toolActivityLabel.js')
const { toolGroupSummary, toolGroupPastSummary } = await vite.ssrLoadModule('/src/components/ChatView/groupBlocks.js')

const tool = (name, input, status = 'done') => ({ type: 'tool', tool: name, input, status })

test('helper tools name the helper for both providers', () => {
  for (const prefix of ['mcp__mobius_control__', 'mobius_control:']) {
    assert.equal(toolCallLabel(tool(`${prefix}spawn_agent`, 'review-auth')), 'Started helper review-auth')
    assert.equal(toolCallLabel(tool(`${prefix}spawn_agent`, 'review-auth', 'running')), 'Starting helper review-auth')
    assert.equal(toolCallLabel(tool(`${prefix}message_agent`, 'review-auth')), 'Messaged helper review-auth')
    assert.equal(toolCallLabel(tool(`${prefix}stop_agent`, 'review-auth')), 'Stopped helper review-auth')
    assert.equal(toolCallLabel(tool(`${prefix}list_agents`, '')), 'Checked helpers')
    assert.equal(toolActivityIcon(effectiveToolName(tool(`${prefix}spawn_agent`, 'x'))), 'agents')
  }
})

test('parallel helpers collapse into one plural line', () => {
  const spawns = ['a', 'b', 'c'].map(n => tool('mcp__mobius_control__spawn_agent', n))
  assert.equal(toolGroupPastSummary(spawns), 'Started helpers')
  assert.equal(toolGroupPastSummary([spawns[0]]), 'Started a helper')
  assert.equal(toolGroupSummary([tool('mobius_control:spawn_agent', 'a', 'running')]), 'Starting a helper')
})

test('other controls read as their activity, not their identifier', () => {
  const cases = [
    ['finish_agent_work', 'Coordinated'],
    ['promote_goal', 'Planned'],
    ['declare_wait', 'Set a wait'],
    ['request_question', 'Asked you'],
    ['send_agent_message', 'Exchanged messages'],
  ]
  for (const [bare, label] of cases) {
    const row = tool(`mcp__mobius_control__${bare}`, 'objective=x')
    assert.equal(toolCallLabel(row), label)
    assert.doesNotMatch(toolGroupPastSummary([tool('Read', 'a.js'), row]), /mcp__|mobius_control/)
  }
})
