/* Resuming sign-in is an activation action, not a side effect of ending a wait. */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { setTimeout as tick } from 'node:timers/promises'
import { api } from '../../api/client.js'
import GithubConnection from '../../components/SettingsView/GithubConnection.jsx'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'

const json = body => new Response(JSON.stringify(body), { status: 200 })
const attempt = { attempt_id: 'a1', user_code: 'TEST-CODE', verification_uri: 'https://github.com/login/device' }
const noop = () => {}

function findNode(node, matches) {
  if (Array.isArray(node)) return node.map(child => findNode(child, matches)).find(Boolean)
  if (!node || typeof node !== 'object') return null
  if (matches(node)) return node
  return findNode(node.props?.children, matches)
}

const findButton = (node, label) => findNode(node, child =>
  child.type === 'button' && child.props?.children === label)
const findSignIn = node => findNode(node, child => child.props?.attempt?.attemptId)
const renderConnection = () => renderHook(() => GithubConnection({
  active: true,
  expanded: true,
  onToggle: noop,
  onExpand: noop,
}))

test('private-access removal guides GitHub revocation and reconnects with public scopes', async t => {
  const original = api.github
  t.after(() => { api.github = original })
  let scopes = ['repo', 'workflow']
  let requestedPrivate = null
  api.github = {
    status: async () => json({ connected: true, login: 'owner', scopes, device_flow_available: true }),
    connectStart: async privateRepos => { requestedPrivate = privateRepos; return json(attempt) },
    connectPoll: async () => { scopes = ['public_repo', 'workflow']; return json({ status: 'complete' }) },
  }
  const view = renderConnection()
  t.after(() => view.unmount())
  await tick(20)
  assert.ok(findButton(view.result.current, 'Remove private access…'))
  findButton(view.result.current, 'Remove private access…').props.onClick()
  assert.ok(findButton(view.result.current, 'I revoked it — reconnect public only'))
  assert.match(JSON.stringify(view.result.current), /github.com\/settings\/applications/)
  await findButton(view.result.current, 'I revoked it — reconnect public only').props.onClick()
  assert.equal(requestedPrivate, false)
  assert.match(JSON.stringify(view.result.current), /Public repositories only/)
  assert.ok(!findButton(view.result.current, 'Remove private access…'))
})

test('private-access removal does not claim success if GitHub grants private scope again', async t => {
  const original = api.github
  t.after(() => { api.github = original })
  api.github = {
    status: async () => json({ connected: true, login: 'owner', scopes: ['repo', 'workflow'], device_flow_available: true }),
    connectStart: async () => json(attempt),
    connectPoll: async () => json({ status: 'complete' }),
  }
  const view = renderConnection()
  t.after(() => view.unmount())
  await tick(20)
  findButton(view.result.current, 'Remove private access…').props.onClick()
  await findButton(view.result.current, 'I revoked it — reconnect public only').props.onClick()
  assert.match(JSON.stringify(view.result.current), /GitHub still granted private-repository access/)
  assert.match(JSON.stringify(view.result.current), /Public and private repositories/)
})

test('public-only connection can request private access and shows the granted result', async t => {
  const original = api.github
  t.after(() => { api.github = original })
  let scopes = ['public_repo', 'workflow']
  let requestedPrivate = null
  api.github = {
    status: async () => json({ connected: true, login: 'owner', scopes, device_flow_available: true }),
    connectStart: async privateRepos => { requestedPrivate = privateRepos; return json(attempt) },
    connectPoll: async () => { scopes = ['repo', 'workflow']; return json({ status: 'complete' }) },
  }
  const view = renderConnection()
  t.after(() => view.unmount())
  await tick(20)
  assert.match(JSON.stringify(view.result.current), /Public repositories only/)
  await findButton(view.result.current, 'Enable private repositories').props.onClick()
  assert.equal(requestedPrivate, true)
  assert.match(JSON.stringify(view.result.current), /Public and private repositories/)
})

for (const connected of [false, true]) {
  test(`cancel a resumed ${connected ? 'private-access upgrade' : 'sign-in'} without restarting its wait`, async t => {
    const original = api.github
    t.after(() => { api.github = original })
    let polls = 0
    let cancelled = false
    let releaseCancel
    api.github = {
      status: async () => json({ connected, device_flow_available: true, active_attempt: cancelled ? null : attempt }),
      connectPoll: async () => { polls++; return json({ status: 'pending', retry_after: 100 }) },
      connectCancel: async id => {
        assert.equal(id, 'a1')
        await new Promise(resolve => { releaseCancel = resolve })
        cancelled = true
        return json({ status: 'cancelled' })
      },
    }
    const view = renderConnection()
    t.after(() => view.unmount())
    await tick(20)
    const panel = findSignIn(view.result.current)
    assert.equal(panel.props.attempt.attemptId, 'a1')
    assert.equal(polls, 1)
    const cancellation = panel.props.onCancel()
    await tick(20)
    assert.equal(polls, 1, 'cancelling must not resume the cached attempt')
    releaseCancel()
    await cancellation
    await tick(20)
    assert.equal(polls, 1)
    assert.ok(!findSignIn(view.result.current))
  })
}

test('a failed cancellation keeps the server-owned attempt visible and resumes its wait', async t => {
  const original = api.github
  t.after(() => { api.github = original })
  let polls = 0
  api.github = {
    status: async () => json({ connected: false, device_flow_available: true, active_attempt: attempt }),
    connectPoll: async () => { polls++; return json({ status: 'pending', retry_after: 100 }) },
    connectCancel: async () => { throw new Error('Cancellation service unavailable') },
  }
  const view = renderConnection()
  t.after(() => view.unmount())
  await tick(20)
  await findSignIn(view.result.current).props.onCancel()
  await tick(20)
  const panel = findSignIn(view.result.current)
  assert.equal(panel.props.attempt?.attemptId, 'a1')
  assert.equal(panel.props.message, 'Cancellation service unavailable')
  assert.equal(polls, 2, 'the still-active server attempt is observed again')
})

test('unconfirmed cancellation never hides the device code when status also fails', async t => {
  const original = api.github
  t.after(() => { api.github = original })
  let unavailable = false
  api.github = {
    status: async () => {
      if (unavailable) throw new Error('offline')
      return json({ connected: false, device_flow_available: true, active_attempt: attempt })
    },
    connectPoll: async () => json({ status: 'pending', retry_after: 100 }),
    connectCancel: async () => { unavailable = true; throw new Error('offline') },
  }
  const view = renderConnection()
  t.after(() => view.unmount())
  await tick(20)
  await findSignIn(view.result.current).props.onCancel()
  const panel = findSignIn(view.result.current)
  assert.equal(panel.props.attempt?.attemptId, 'a1')
  assert.equal(panel.props.message, 'offline')
  assert.equal(panel.props.cancelling, false)
})

test('unmount during start aborts its request and cannot create a later orphan poll', async t => {
  const original = api.github
  t.after(() => { api.github = original })
  let releaseStart, startSignal
  let polls = 0
  api.github = {
    status: async () => json({ connected: false, device_flow_available: true }),
    connectStart: async (_private, options) => {
      startSignal = options.signal
      await new Promise(resolve => { releaseStart = resolve })
      return json(attempt)
    },
    connectPoll: async () => { polls++; return json({ status: 'complete' }) },
  }
  const view = renderConnection()
  await tick(20)
  const starting = findButton(view.result.current, 'Connect GitHub').props.onClick()
  view.unmount()
  releaseStart()
  await starting
  assert.equal(startSignal?.aborted, true)
  assert.equal(polls, 0)
})

test('unmount during cancellation owns reconciliation and cannot resume a hidden poll', async t => {
  const original = api.github
  t.after(() => { api.github = original })
  let releaseCancel, cancelSignal
  let polls = 0
  api.github = {
    status: async () => json({ connected: false, device_flow_available: true, active_attempt: attempt }),
    connectPoll: async () => { polls++; return json({ status: 'pending', retry_after: 100 }) },
    connectCancel: async (_id, options) => {
      cancelSignal = options.signal
      await new Promise(resolve => { releaseCancel = resolve })
      throw new Error('offline')
    },
  }
  const view = renderConnection()
  await tick(20)
  const cancelling = findSignIn(view.result.current).props.onCancel()
  view.unmount()
  releaseCancel()
  await cancelling
  await tick(20)
  assert.equal(cancelSignal?.aborted, true)
  assert.equal(polls, 1, 'only the original visible poll may run')
})
