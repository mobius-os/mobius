import { api, jsonOrThrow } from '../api/client.js'

export const APP_OWNER_ACTION = 'app.owner-action'

export function createAppOwnerActionProvider({ appId, present, request = api.ownerActions } = {}) {
  return {
    version: 1, exclusive: true, onDeactivate: 'cancel',
    open({ input, channel }) {
      if (!Number.isInteger(Number(appId)) || !present || !input ||
          typeof input.action !== 'string' || !/^[a-z0-9_-]{1,64}$/.test(input.action) ||
          Object.keys(input).some(key => !['action', 'context'].includes(key))) {
        throw new TypeError('Open a reviewed owner action from the app.')
      }
      let cancelled = false, submitted = false, prompt = null
      const clear = () => present(null)
      const cancel = () => {
        if (cancelled) return
        cancelled = true
        clear()
        if (prompt && !submitted) request.cancel(appId, prompt.ticket).catch(() => {})
        channel.error(Object.assign(new Error('Owner action closed. If already submitted, check its status in the app.'), { code: 'aborted' }))
      }
      Promise.resolve(request.prepare(appId, input.action, input.context || {}))
        .then(response => jsonOrThrow(response, 'Could not open trusted input'))
        .then(value => {
          prompt = value
          if (cancelled) { request.cancel(appId, prompt.ticket).catch(() => {}); return }
          channel.ready({})
          present({
            prompt, cancel,
            async submit(fields) {
              if (submitted || cancelled) { for (const key of Object.keys(fields)) fields[key] = ''; return }
              submitted = true
              try {
                const result = await jsonOrThrow(await request.submit(appId, prompt.ticket, fields), 'Outcome unknown. Check the app before trying again.')
                if (!cancelled) { clear(); channel.result({ status: result.status, message: result.message }) }
              } catch {
                if (!cancelled) { clear(); channel.error(new Error('Outcome unknown. Check the app before trying again.')) }
              } finally { for (const key of Object.keys(fields)) fields[key] = '' }
            },
          })
        }).catch(() => { if (!cancelled) channel.error(new Error('Could not open the trusted form. Refresh Möbius and try again.')) })
      return { control(action) { if (action === 'cancel') cancel() } }
    },
  }
}
