import test from 'node:test'
import assert from 'node:assert/strict'
import { Deployments } from '../../components/SettingsView/identity/IdentityAccount.jsx'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'

function nodes(node, predicate) {
  if (Array.isArray(node)) return node.flatMap(child => nodes(child, predicate))
  if (!node || typeof node !== 'object') return []
  return [...(predicate(node) ? [node] : []), ...nodes(node.props?.children, predicate)]
}
const rows = view => nodes(view.result.current, node => node.props?.className?.startsWith('id-deployment id-deployment--'))
const names = view => nodes(view.result.current, node => node.props?.className === 'id-deploy-name').map(node => node.props.children)
const instance = (id, status, url = null) => ({
  id, name: id, status, url, railway_url: `https://railway.com/project/${id}`,
  actions: { delete: true, edit_resources: false, retry: false },
  resources: { cpu: null, memory_mb: null, volume_size_mb: null },
})
function renderDeployments(t, items, instances) {
  const deletions = []
  const recoveries = []
  const view = renderHook(() => Deployments({
    token: 'fixture-token', items,
    railway: { railway_access: 'available', connection: { connected: true }, instances },
    selfHosted: true, onDelete: target => deletions.push(target),
    onManage: (target, section) => recoveries.push([target, section]),
  }))
  t.after(() => view.unmount())
  return { view, deletions, recoveries }
}

for (const items of [[], [{ id: 'local', name: 'Local', current: true, status: 'active', url: 'http://localhost:8000' }]]) {
  test(`distinct URL-less deployments remain visible ${items.length ? 'beside a local current deployment' : 'without linked deployments'}`, t => {
    const queued = instance('queued', 'queued')
    const failed = instance('failed', 'error')
    const { view, deletions, recoveries } = renderDeployments(t, items, [queued, failed])
    assert.deepEqual(names(view), [...items.map(item => item.name), 'queued', 'failed'])
    assert.equal(rows(view).length, items.length + 2)
    if (items.length) {
      assert.equal(nodes(rows(view)[0], node => node.props?.className === 'id-current-chip').length, 1)
      assert.equal(nodes(rows(view)[0], node => node.props?.className?.startsWith('id-deploy-buttons')).length, 0)
    }
    // These callbacks select the actual managed instance; no remote operation
    // runs, and queued cancellation keeps its distinct label.
    nodes(view.result.current, node => node.props?.['aria-label'] === 'Cancel deployment of queued')[0].props.onClick()
    nodes(view.result.current, node => node.props?.['aria-label'] === 'Delete failed')[0].props.onClick()
    nodes(view.result.current, node => node.props?.['aria-label'] === 'Recover failed')[0].props.onClick()
    assert.deepEqual(deletions, [queued, failed])
    assert.deepEqual(recoveries, [[failed, 'recovery']])
  })
}

for (const match of ['id', 'https-origin']) {
  test(`${match} still deduplicates a linked deployment and maps its controls to the managed instance`, t => {
    const managed = instance('managed', 'error', match === 'id' ? null : 'https://example.invalid/new-path')
    const item = {
      id: match === 'id' ? managed.id : 'linked', name: 'Linked name', current: true, status: 'active',
      url: match === 'id' ? null : 'https://example.invalid/old-path',
    }
    const { view, deletions, recoveries } = renderDeployments(t, [item], [managed])
    assert.equal(rows(view).length, 1)
    assert.deepEqual(names(view), ['managed'])
    assert.equal(nodes(view.result.current, node => node.props?.className === 'id-current-chip').length, 1)
    nodes(view.result.current, node => node.props?.['aria-label'] === 'Delete managed')[0].props.onClick()
    nodes(view.result.current, node => node.props?.['aria-label'] === 'Recover managed')[0].props.onClick()
    assert.deepEqual(deletions, [managed])
    assert.deepEqual(recoveries, [[managed, 'recovery']])
  })
}
