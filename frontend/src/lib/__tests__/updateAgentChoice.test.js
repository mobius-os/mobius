/** Update choices are explicit, cancel-safe and use the same live model metadata. */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import { authQueries, modelQueries } from '../../hooks/queries.js'
import useUpdateAgentChoice, { UpdateAgentPicker } from '../../components/SettingsView/useUpdateAgentChoice.jsx'

test('closing or unmounting the job picker cancels without a selection', async () => {
  const hook = renderHook(useUpdateAgentChoice)
  const first = hook.result.current.chooseAgent()
  assert.equal(hook.result.current.chooseAgent(), first, 'double click shares the one choice')
  hook.result.current.agentPicker.props.onChoice(null)
  assert.equal(await first, null)
  assert.equal(hook.result.current.agentPicker, null)
  const second = hook.result.current.chooseAgent()
  hook.unmount()
  assert.equal(await second, null)
})

test('confirming resolves the exact choice and permits another independent job', async () => {
  const hook = renderHook(useUpdateAgentChoice)
  const request = hook.result.current.chooseAgent()
  const choice = { provider: 'codex', model: 'codex-test', effort: 'high' }
  hook.result.current.agentPicker.props.onChoice(choice)
  assert.deepEqual(await request, choice)
  const next = hook.result.current.chooseAgent()
  assert.notEqual(request, next)
  hook.unmount()
  assert.equal(await next, null)
})

function mockCatalog(t, prefs = { hidden_ids: ['hidden'], recent_ids: ['claude-test'] }) {
  const query = data => ({ data, isPending: false, isError: false })
  t.mock.method(modelQueries.registry, 'useQuery', () => query({
    codex: [{ id: 'codex-test', label: 'Codex Test', effort_levels: ['low', 'high'] }, { id: 'hidden' }],
    claude: [{ id: 'claude-test', label: 'Claude Test', effort_levels: ['medium'] }],
    mobius: [{ id: 'offline-model' }],
  }))
  t.mock.method(modelQueries.prefs, 'useQuery', () => query({
    selected_agent: { provider: 'codex', model: 'codex-test', effort: 'high' }, ...prefs,
  }))
  t.mock.method(authQueries.provider.statuses, 'useQuery', () => query({
    codex: { configured: true }, claude: { configured: true }, mobius: { configured: true, available: false },
  }))
}

test('the update picker respects MRU, hidden models, availability and model effort metadata', t => {
  mockCatalog(t)
  const choices = []
  const hook = renderHook(() => UpdateAgentPicker({ onChoice: value => choices.push(value) }))
  let props = hook.result.current.props
  assert.equal(props.collapsible, true, 'the catalogue stays collapsed behind the latest agent')
  assert.equal(props.model, 'codex-test', 'the default is the saved chat agent, not the first catalogue row')
  assert.deepEqual(props.groups.flatMap(group => group.models.map(model => model.id)), ['claude-test', 'codex-test'])
  assert.equal(props.effort, 'high')
  props.onPick('claude', 'claude-test', props.groups[0].models[0])
  assert.deepEqual(choices, [], 'a staged pick does not start the agent')
  props = hook.result.current.props
  assert.deepEqual(props.efforts.map(row => row.value), ['medium'])
  props.onConfirm()
  assert.deepEqual(choices, [{ provider: 'claude', model: 'claude-test', effort: 'medium' }])
  hook.unmount()
})

test('an unavailable preference read cannot silently launch a different choice', t => {
  mockCatalog(t, { unavailable: true, hidden_ids: [], recent_ids: [] })
  const hook = renderHook(() => UpdateAgentPicker({ onChoice: () => {} }))
  assert.equal(hook.result.current.props.confirmDisabled, true)
  assert.match(hook.result.current.props.notice, /Could not load/)
  hook.unmount()
})


test('a cached agent cannot be confirmed while its fresh read is in flight', t => {
  mockCatalog(t)
  t.mock.method(modelQueries.prefs, 'useQuery', () => ({
    data: { hidden_ids: [], selected_agent: { provider: 'codex', model: 'codex-test', effort: 'low' } },
    isPending: false, isFetching: true, isError: false,
  }))
  const hook = renderHook(() => UpdateAgentPicker({ onChoice: () => {} }))
  assert.equal(hook.result.current.props.confirmDisabled, true)
  assert.match(hook.result.current.props.notice, /Loading/)
  hook.unmount()
})
