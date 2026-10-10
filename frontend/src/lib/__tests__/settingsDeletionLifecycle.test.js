import test from 'node:test'
import assert from 'node:assert/strict'
import { DeleteDeploymentModal, DeletionRecoverySection } from '../../components/SettingsView/identity/IdentityAccount.jsx'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'

const target = {
  id: 'mob_example', name: 'Example', status: 'delete_failed',
  railway_url: 'https://railway.com/project/example', actions: { retry: true },
}
const noop = () => {}
function find(node, predicate) {
  if (Array.isArray(node)) return node.map(child => find(child, predicate)).find(Boolean)
  if (!node || typeof node !== 'object') return null
  return predicate(node) ? node : find(node.props?.children, predicate)
}
const retryButton = node => find(node, child => child.type === 'button' && child.props.className === 'id-btn id-btn--danger')
const recovery = node => find(node, child => child.type === DeletionRecoverySection)
const flush = () => new Promise(resolve => setImmediate(resolve))

test('delete retry is unavailable until present diagnosis and advertised eligibility, and a recheck clears confirmation', t => {
  const oldDocument = globalThis.document
  globalThis.document = { activeElement: null }
  t.after(() => { globalThis.document = oldDocument })
  const props = { instance: target, token: 'token', onClose: noop, onRetry: noop, onDelete: noop, onConfirmAbsent: noop }
  const view = renderHook(next => DeleteDeploymentModal(next), props)
  t.after(() => view.unmount())
  assert.equal(retryButton(view.result.current).props.disabled, true)
  for (const state of ['missing', 'missing_unconfirmed', 'authorization', 'unknown']) {
    recovery(view.result.current).props.onDiagnosis({ state })
    assert.equal(retryButton(view.result.current).props.disabled, true)
  }
  recovery(view.result.current).props.onDiagnosis({ state: 'present' })
  assert.equal(retryButton(view.result.current).props.disabled, false)
  retryButton(view.result.current).props.onClick()
  assert.match(JSON.stringify(view.result.current), /Try removing this Railway project again/)
  recovery(view.result.current).props.onDiagnosis(null)
  assert.equal(retryButton(view.result.current).props.disabled, true)
  assert.doesNotMatch(JSON.stringify(view.result.current), /Try removing this Railway project again/)
  recovery(view.result.current).props.onDiagnosis({ state: 'present' })
  view.rerender({ ...props, instance: { ...target, actions: { retry: false } } })
  assert.equal(retryButton(view.result.current).props.disabled, true)
})

for (const status of [200, 404, 503]) {
  test(`unsupported or failed deletion check (${status}) clears retry admission without losing Railway inspection`, async t => {
    const diagnoses = []
    const onDiagnosis = diagnosis => diagnoses.push(diagnosis)
    t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ detail: 'Check unavailable' }), { status }))
    const view = renderHook(() => DeletionRecoverySection({
      token: 'token', instance: target, pending: '', onConfirmAbsent: noop, onDiagnosis,
    }))
    t.after(() => view.unmount())
    await flush()
    assert.deepEqual(diagnoses, [null])
    assert.ok(find(view.result.current, node => node.type === 'button' && Array.isArray(node.props.children) && node.props.children.includes('Open Railway ')))
    assert.doesNotMatch(JSON.stringify(view.result.current), /Try deleting again/)
  })
}

for (const state of ['unknown', 'present']) {
  test(`confirmed retry reconciles before dispatch and only proceeds for ${state}`, async t => {
    const oldDocument = globalThis.document
    globalThis.document = { activeElement: null }
    t.after(() => { globalThis.document = oldDocument })
    const calls = []
    let retried = 0
    const current = {
      ...target, url: null, current_step: null, last_error: null,
      resources: { cpu: null, memory_mb: null, volume_size_mb: null, plan: 'hobby' },
      actions: { edit_resources: false, retry: true, delete: true },
    }
    t.mock.method(globalThis, 'fetch', async url => {
      calls.push(url)
      const body = calls.length === 1 ? {
        railway_access: 'available',
        connection: { connected: true, account: 'owner', workspace: 'workspace', plan: 'hobby', deploy_blocked: '' },
        instances: [current],
      } : { state, message: 'Railway project check.', can_confirm_absent: false }
      return new Response(JSON.stringify(body))
    })
    const view = renderHook(() => DeleteDeploymentModal({
      token: 'token', instance: target, onClose: noop,
      onRetry: async () => {
        assert.equal(calls.length, 2)
        retried++
      }, onDelete: () => assert.fail('wrong delete flow'), onConfirmAbsent: noop,
    }))
    t.after(() => view.unmount())
    recovery(view.result.current).props.onDiagnosis({ state: 'present' })
    retryButton(view.result.current).props.onClick()
    await retryButton(view.result.current).props.onClick()
    assert.deepEqual(calls, ['/api/identity/railway', '/api/identity/railway/deployments/mob_example/deletion'])
    assert.equal(retried, state === 'present' ? 1 : 0)
    if (state === 'unknown') assert.match(JSON.stringify(view.result.current), /deletion was not retried/)
  })
}

for (const status of ['ready', 'queued']) {
  test(`ordinary ${status === 'queued' ? 'build cancellation' : 'deletion'} keeps the original confirmed delete path`, async t => {
    const oldDocument = globalThis.document
    globalThis.document = { activeElement: null }
    t.after(() => { globalThis.document = oldDocument })
    let deleted = null
    t.mock.method(globalThis, 'fetch', () => assert.fail('ordinary deletion must not enter retry reconciliation'))
    const view = renderHook(() => DeleteDeploymentModal({
      token: 'token', instance: { ...target, status }, onClose: noop,
      onRetry: () => assert.fail('wrong retry flow'), onDelete: async id => { deleted = id }, onConfirmAbsent: noop,
    }))
    t.after(() => view.unmount())
    assert.equal(retryButton(view.result.current).props.disabled, false)
    retryButton(view.result.current).props.onClick()
    await retryButton(view.result.current).props.onClick()
    assert.equal(deleted, target.id)
  })
}

test('a late deletion diagnosis for an old target cannot re-admit retry', async t => {
  const diagnoses = []
  const requests = []
  t.mock.method(globalThis, 'fetch', (_url, options) => new Promise(resolve => requests.push({ resolve, options })))
  const onDiagnosis = diagnosis => diagnoses.push(diagnosis)
  const props = { token: 'token', instance: target, pending: '', onConfirmAbsent: noop, onDiagnosis }
  const view = renderHook(next => DeletionRecoverySection(next), props)
  t.after(() => view.unmount())
  view.rerender({ ...props, instance: { ...target, railway_url: 'https://railway.com/project/other' } })
  assert.equal(requests[0].options.signal.aborted, true)
  requests[0].resolve(new Response(JSON.stringify({ state: 'present', message: 'Old project exists.', can_confirm_absent: false })))
  await flush()
  assert.deepEqual(diagnoses, [null, null])
})

// Resolve aborted reads deliberately: cancellation must fence the destructive
// callback even when a transport ignores AbortSignal or a response is queued.
for (const stage of ['inventory', 'diagnosis']) {
  for (const departure of ['unmount', 'token-change', 'target-change', 'project-url-change', 'retry-disabled', 'status-change']) {
    test(`leaving the retry modal by ${departure} during ${stage} prevents late deletion dispatch`, async t => {
      const oldDocument = globalThis.document
      globalThis.document = { activeElement: null }
      t.after(() => { globalThis.document = oldDocument })
      const requests = []
      t.mock.method(globalThis, 'fetch', (url, options) => new Promise(resolve => requests.push({ url, options, resolve })))
      let retried = 0
      let closed = 0
      const props = {
        instance: target, token: 'token', onRetry: async () => { retried++ },
        onDelete: () => assert.fail('wrong deletion path'), onConfirmAbsent: noop,
        onClose: () => { closed++ },
      }
      const view = renderHook(next => DeleteDeploymentModal(next), props)
      t.after(() => view.unmount())
      recovery(view.result.current).props.onDiagnosis({ state: 'present' })
      retryButton(view.result.current).props.onClick()
      const running = retryButton(view.result.current).props.onClick()
      const inventory = {
        railway_access: 'available',
        connection: { connected: true, account: 'owner', workspace: 'workspace', plan: 'hobby', deploy_blocked: '' },
        instances: [{
          ...target, url: null, current_step: null, last_error: null,
          resources: { cpu: null, memory_mb: null, volume_size_mb: null, plan: 'hobby' },
          actions: { edit_resources: false, retry: true, delete: true },
        }],
      }
      assert.equal(requests.length, 1)
      const signal = requests[0].options.signal
      assert.equal(signal.aborted, false)
      if (stage === 'diagnosis') {
        requests[0].resolve(new Response(JSON.stringify(inventory)))
        await flush()
        assert.equal(requests.length, 2)
        assert.equal(requests[1].options.signal, signal)
      }
      if (departure === 'unmount') view.unmount()
      else {
        const changes = {
          'token-change': { token: 'different-account-token' },
          'target-change': { instance: { ...target, id: 'mob_other' } },
          'project-url-change': { instance: { ...target, railway_url: 'https://railway.com/project/other' } },
          'retry-disabled': { instance: { ...target, actions: { retry: false } } },
          'status-change': { instance: { ...target, status: 'deleting' } },
        }
        view.rerender({ ...props, ...changes[departure] })
      }
      assert.equal(signal.aborted, true)
      const lateBody = stage === 'inventory' ? inventory
        : { state: 'present', message: 'Project exists.', can_confirm_absent: false }
      requests.at(-1).resolve(new Response(JSON.stringify(lateBody)))
      await running
      assert.equal(retried, 0)
      assert.equal(closed, 0)
      assert.equal(requests.length, stage === 'inventory' ? 1 : 2)
      if (departure !== 'unmount') {
        if (departure !== 'status-change') assert.equal(retryButton(view.result.current).props.disabled, true)
        assert.doesNotMatch(JSON.stringify(view.result.current), /Deletion retry was cancelled/)
      }
    })
  }
}
