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
    ['finish_agent_work', 'outcome=Merged', 'Settled a claim: Merged'],
    ['promote_goal', 'objective=Ship the release', 'Started a Goal: Ship the release'],
    ['declare_wait', 'description=CI passes, condition_owner=GitHub', 'Set a wait: CI passes'],
    ['request_question', "questions=[{'question': 'x'}]", 'Asked you'],
    ['send_agent_message', "recipients=['a'], kind=note, body=Heads up", 'Messaged another chat: Heads up'],
  ]
  for (const [bare, input, label] of cases) {
    const row = tool(`mcp__mobius_control__${bare}`, input)
    assert.equal(toolCallLabel(row), label)
    assert.doesNotMatch(toolGroupPastSummary([tool('Read', 'a.js'), row]), /mcp__|mobius_control/)
  }
})

test('a restart request row still reads as words, not an identifier', () => {
  assert.equal(toolCallLabel(tool('mcp__mobius_control__request_restart', '')), 'Asked to restart Möbius')
})

test('a Goal update names what it did to the Goal', () => {
  const goal = (input, status) => toolCallLabel(tool('mcp__mobius_control__update_goal', input, status))
  assert.equal(goal("tasks=[{'id': 'a'}]"), 'Updated the plan')
  assert.equal(goal("tasks=[{'id': 'a'}], complete=Verified"), 'Completed the Goal')
  assert.equal(goal('next_action=Run the check'), 'Left the next step')
  assert.equal(goal('', 'running'), 'Reading the plan')
})

test('JSON Goal arguments describe the operation, not a plan read', () => {
  for (const prefix of ['mcp__mobius_control__', 'mobius_control:']) {
    const goal = (args, status = 'done', extra = {}) => toolCallLabel({
      ...tool(`${prefix}update_goal`, JSON.stringify(args, null, 2), status), ...extra,
    })
    assert.equal(goal({ tasks: [{ id: 'activate', status: 'completed' }], complete: 'Verified', finished_claims: ['key'] }), 'Completed the Goal')
    assert.equal(goal({ complete: 'Verified' }, 'running'), 'Completing the Goal')
    assert.equal(goal({ complete: 'Verified' }, 'done', { output_exit_code: 1 }), 'Could not complete the Goal')
    assert.equal(goal({ tasks: [{ id: 'a' }] }), 'Updated the plan')
    assert.equal(goal({ tasks: [{ id: 'a' }] }, 'failed'), 'Could not update the plan')
    assert.equal(goal({ next_action: 'Run the check' }), 'Left the next step')
    assert.equal(goal({}), 'Read the plan')
    assert.equal(goal({ complete: null }), 'Read the plan')
    assert.equal(goal({ next_action: 'Text mentioning complete=Verified' }), 'Left the next step')
  }
})

test('JSON control details preserve the same wording as summarized inputs', () => {
  assert.equal(toolCallLabel(tool('mobius_control:promote_goal', JSON.stringify({ objective: 'Ship the release' }))), 'Started a Goal: Ship the release')
  assert.equal(toolCallLabel(tool('mobius_control:apply_app', JSON.stringify({ source_dir: '/data/apps/notes' }))), 'Applied app notes')
  assert.equal(toolCallLabel(tool('mobius_control:update_goal', 'complete=Verified', 'failed')), 'Could not complete the Goal')
})

test('app and screenshot tools name their target', () => {
  assert.equal(
    toolCallLabel(tool('mobius_control:screenshot', 'route=/shell/?app=4, content_only=True')),
    'Took a screenshot of /shell/?app=4',
  )
  assert.equal(toolCallLabel(tool('mcp__mobius_control__apply_app', 'source_dir=/data/apps/notes')), 'Applied app notes')
  assert.equal(toolCallLabel(tool('mcp__mobius_control__notify_owner', 'title=Ready, body=Built')), 'Notified you: Ready')
  assert.equal(toolActivityIcon(effectiveToolName(tool('mcp__mobius_control__screenshot', ''))), 'image')
})

test('a screenshot taken by app id names the app instead of ending at "of"', () => {
  assert.equal(toolCallLabel(tool('mcp__mobius_control__screenshot', 'app_id=9')), 'Took a screenshot of app 9')
  assert.equal(toolCallLabel(tool('mobius_control:screenshot', JSON.stringify({ app_id: 15 }))), 'Took a screenshot of app 15')
  assert.equal(toolCallLabel(tool('mcp__mobius_control__screenshot', 'app_id=9', 'running')), 'Taking a screenshot of app 9')
  assert.equal(toolCallLabel(tool('mcp__mobius_control__screenshot', '')), 'Took a screenshot')
})

test("an installed app's own tool reads as app work, not an identifier", () => {
  const row = tool('mcp__mobius_control__memory_search', 'prompt=launch plans, limit=5')
  assert.equal(toolCallLabel(row), 'Memory search: launch plans')
  assert.equal(toolGroupPastSummary([row]), 'Used app tools')
})
