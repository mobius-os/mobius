import test from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'

const loader = new URL('./vite-env-loader.mjs', import.meta.url).href
const client = new URL('../../components/SettingsView/identity/identity-client.js', import.meta.url).href

for (const base of ['/', '/proxy/8001/']) {
  test(`native identity and avatar transports retain the shell deployment base ${base}`, () => {
    // A fresh module graph runs with an actual non-root Vite environment;
    // changing an already-imported BASE would only exercise a root-only stub.
    const script = `
      import assert from 'node:assert/strict'
      import { identityRequest, fetchAvatarBlob } from ${JSON.stringify(client)}
      const controller = new AbortController()
      const calls = []
      globalThis.fetch = async (url, options) => {
        assert.equal(options.signal, controller.signal)
        calls.push({ url, headers: options.headers })
        if (url.endsWith('/avatar')) return new Response(new Blob(['photo'], { type: 'image/png' }))
        return new Response(JSON.stringify({ state: 'present', message: 'Project exists.', can_confirm_absent: false }))
      }
      const options = { signal: controller.signal, headers: { 'X-Check': 'read' } }
      const diagnosis = await identityRequest('test-token', '/railway/deployments/mob_example/deletion?check=1', options)
      assert.equal(diagnosis.state, 'present')
      await identityRequest('test-token', '/api/identity/custom?full=1', options)
      const avatar = await fetchAvatarBlob('test-token', controller.signal)
      assert.equal(avatar.type, 'image/png')
      console.log(JSON.stringify(calls))
    `
    const result = spawnSync(process.execPath, ['--disable-warning=ExperimentalWarning', '--loader', loader, '--input-type=module', '-e', script], {
      encoding: 'utf8',
      env: { ...process.env, MOBIUS_TEST_BASE_URL: base },
      timeout: 30_000,
    })
    assert.equal(result.status, 0, result.stderr || String(result.error))
    const prefix = base.replace(/\/$/, '')
    assert.deepEqual(JSON.parse(result.stdout), [
      { url: `${prefix}/api/identity/railway/deployments/mob_example/deletion?check=1`, headers: { Authorization: 'Bearer test-token', 'X-Check': 'read' } },
      { url: `${prefix}/api/identity/custom?full=1`, headers: { Authorization: 'Bearer test-token', 'X-Check': 'read' } },
      { url: `${prefix}/api/identity/avatar`, headers: { Authorization: 'Bearer test-token' } },
    ])
  })
}
