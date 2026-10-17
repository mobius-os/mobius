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
  const props = { instance: target, onClose: noop, onRetry: noop, onDelete: noop, onConfirmAbsent: noop }
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
      instance: target, pending: '', onConfirmAbsent: noop, onDiagnosis,
    }))
    t.after(() => view.unmount())
    await flush()
    assert.deepEqual(diagnoses, [null])
    assert.ok(find(view.result.current, node => node.type === 'button' && Array.isArray(node.props.children) && node.props.children.includes('Open Railway ')))
    assert.doesNotMatch(JSON.stringify(view.result.current), /Try deleting again/)
  })
}

test('a confirmed retry after a present diagnosis asks the account service once', async t => {
  const oldDocument = globalThis.document
  globalThis.document = { activeElement: null }
  t.after(() => { globalThis.document = oldDocument })
  const retried = []
  let closed = 0
  const view = renderHook(() => DeleteDeploymentModal({
    instance: target, onClose: () => { closed++ }, onRetry: async id => { retried.push(id) },
    onDelete: () => assert.fail('wrong deletion path'), onConfirmAbsent: noop,
  }))
  t.after(() => view.unmount())
  recovery(view.result.current).props.onDiagnosis({ state: 'present' })
  retryButton(view.result.current).props.onClick()
  await retryButton(view.result.current).props.onClick()
  assert.deepEqual(retried, [target.id])
  assert.equal(closed, 1)
})

for (const status of ['ready', 'queued']) {
  test(`ordinary ${status === 'queued' ? 'build cancellation' : 'deletion'} keeps the original confirmed delete path`, async t => {
    const oldDocument = globalThis.document
    globalThis.document = { activeElement: null }
    t.after(() => { globalThis.document = oldDocument })
    let deleted = null
    t.mock.method(globalThis, 'fetch', () => assert.fail('ordinary deletion must not enter retry reconciliation'))
    const view = renderHook(() => DeleteDeploymentModal({
      instance: { ...target, status }, onClose: noop,
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
  const props = { instance: target, pending: '', onConfirmAbsent: noop, onDiagnosis }
  const view = renderHook(next => DeletionRecoverySection(next), props)
  t.after(() => view.unmount())
  view.rerender({ ...props, instance: { ...target, railway_url: 'https://railway.com/project/other' } })
  assert.equal(requests[0].options.signal.aborted, true)
  requests[0].resolve(new Response(JSON.stringify({ state: 'present', message: 'Old project exists.', can_confirm_absent: false })))
  await flush()
  assert.deepEqual(diagnoses, [null, null])
})
