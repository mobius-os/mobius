import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ExternalLink } from '@openai/apps-sdk-ui/components/Icon'
import { api } from '../../api/client.js'
import { authQueries, modelQueries } from '../../hooks/queries.js'
import { detailToMessage } from '../../lib/errorDetail.js'

const AI_STUDIO_KEYS_URL = 'https://aistudio.google.com/app/apikey'
// The vault key the built-in Gemini provider reads (providers.GEMINI_KEY_NAME).
const KEY_NAME = 'gemini'
const GEMINI_HOST = 'generativelanguage.googleapis.com'

/**
 * The vault record for the key: kept from agents, carried in Google's own
 * header, and usable only on Gemini's model endpoints (the vault applies a
 * key's sites to every use, the owner's check included).
 */
export function geminiKeyRecord(value) {
  return {
    value,
    inject: { type: 'header', name: 'x-goog-api-key' },
    grants: {
      agents: false,
      sites: [{ host: GEMINI_HOST, methods: ['GET', 'POST'], paths: ['/v1beta/models', '/v1beta/models/*'] }],
    },
  }
}

/** One small read Google answers only for a valid key; null when the vault could not ask. */
async function keyWorks() {
  const response = await api.keys.use(KEY_NAME, `${GEMINI_HOST}/v1beta/models?pageSize=1`)
  if (response.headers.get('X-Vault-Denied')) return null
  return response.ok
}

/**
 * Google allows no subscription sign-in for third-party agents, so Gemini
 * connects with an AI Studio key. It is entered here, by the owner, and goes
 * straight to the key vault; Möbius sends it only to Google's Gemini API.
 */
export default function GeminiAuth({ onConnected }) {
  const queryClient = useQueryClient()
  const [value, setValue] = useState('')
  const [status, setStatus] = useState('idle') // idle | saving | complete
  const [error, setError] = useState('')

  async function connect(event) {
    event.preventDefault()
    const key = value.trim()
    if (!key) return
    setStatus('saving')
    setError('')
    try {
      const saved = await api.keys.put(KEY_NAME, geminiKeyRecord(key))
      if (!saved.ok) {
        const data = await saved.json().catch(() => ({}))
        throw new Error(detailToMessage(data.detail, 'Could not save the key.'))
      }
      setValue('')
      const works = await keyWorks()
      if (works !== true) {
        await api.keys.revoke(KEY_NAME)
        throw new Error(works === null
          ? 'Möbius could not check the key with Google. Please try again.'
          : 'Google did not accept this key. Check it in AI Studio and try again.')
      }
      void authQueries.provider.statuses.invalidate(queryClient)
      void modelQueries.registry.invalidate(queryClient)
      setStatus('complete')
      onConnected?.()
    } catch (failure) {
      setStatus('idle')
      setError(failure.message || 'Could not connect. Please try again.')
    }
  }

  if (status === 'complete') {
    return <span className="pa__success" role="status">Connected to Google Gemini</span>
  }

  return (
    <form className="pa__form" onSubmit={connect}>
      <p className="pa__muted">
        Google lets third-party apps use Gemini with an API key, not a Google
        AI Pro or Ultra sign-in. Create a key in Google AI Studio and paste it
        here. It is saved in your key vault and sent only to Google.
      </p>
      <a className="pa__btn pa__btn--sm pa__external-link" href={AI_STUDIO_KEYS_URL} target="_blank" rel="noopener noreferrer">
        Open Google AI Studio
        <ExternalLink width={14} height={14} aria-hidden="true" focusable="false" />
      </a>
      <label className="pa__field">
        <span className="pa__input-label">AI Studio API key</span>
        <input
          className="pa__input"
          type="password"
          autoComplete="off"
          spellCheck={false}
          enterKeyHint="go"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          disabled={status === 'saving'}
        />
      </label>
      <button type="submit" className="pa__btn" disabled={status === 'saving' || !value.trim()}>
        {status === 'saving' ? 'Checking key…' : 'Connect'}
      </button>
      {error && <p className="pa__error" role="alert">{error}</p>}
    </form>
  )
}
