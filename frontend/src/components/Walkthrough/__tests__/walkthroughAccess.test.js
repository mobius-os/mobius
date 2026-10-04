import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { capabilityRows, installReviewedApp, previewAppAccess } from '../walkthroughAccess.js'

const realFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = realFetch })

function stubFetch(status, body) {
  const calls = []
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), body: init?.body ? JSON.parse(init.body) : null })
    return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
  }
  return calls
}

test('access_review_lists_a_known_grant_with_its_plain_wording', () => {
  const rows = capabilityRows({ data: { shared_memory: 'read', chat_logs: { effective: 'summary' } } })
  assert.deepEqual(rows.map(row => row.label), ['Chat history', 'Shared memory'])
  assert.match(rows[1].summary, /cannot change it/)
})

test('access_review_never_hides_a_grant_it_has_no_wording_for', () => {
  const rows = capabilityRows({ data: { brand_new_grant: true } })
  assert.equal(rows.length, 1)
  assert.equal(rows[0].tag, 'Review')
  assert.match(rows[0].summary, /brand_new_grant/)
})

test('access_review_of_an_app_without_grants_is_empty_not_missing', () => {
  assert.deepEqual(capabilityRows({ data: {} }), [])
  assert.deepEqual(capabilityRows(null), [])
})

test('previewing_an_apps_access_only_reads_its_manifest', async () => {
  const calls = stubFetch(200, { capability_contract: { data: {} }, capability_digest: 'd1' })
  const preview = await previewAppAccess('https://example.test/mobius.json')
  assert.equal(preview.capability_digest, 'd1')
  assert.deepEqual(calls.map(call => call.url.replace(/^.*\/api/, '')), ['/apps/preview'])
  assert.deepEqual(calls[0].body, { manifest_url: 'https://example.test/mobius.json' })
})

test('installing_sends_exactly_the_digest_the_owner_reviewed', async () => {
  const calls = stubFetch(200, { id: 7 })
  assert.deepEqual(await installReviewedApp('https://example.test/mobius.json', 'd1'), { status: 'installed' })
  assert.deepEqual(calls[0].body, { manifest_url: 'https://example.test/mobius.json', reviewed_capability_digest: 'd1' })
})

test('an_app_whose_access_changed_after_review_comes_back_for_a_new_review', async () => {
  stubFetch(409, { detail: { code: 'capability_changed', capability_contract: { data: { manage_apps: true } }, capability_digest: 'd2' } })
  const result = await installReviewedApp('https://example.test/mobius.json', 'd1')
  assert.equal(result.status, 'changed')
  assert.equal(result.preview.capability_digest, 'd2')
  assert.deepEqual(capabilityRows(result.preview.capability_contract).map(row => row.label), ['Installed apps'])
})

test('a_failed_install_reports_the_servers_reason', async () => {
  stubFetch(400, { detail: 'The manifest is invalid.' })
  await assert.rejects(installReviewedApp('https://example.test/mobius.json', 'd1'), /manifest is invalid/)
})
