import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ExternalLink } from '@openai/apps-sdk-ui/components/Icon'
import { api } from '../../api/client.js'
import { authQueries, modelQueries } from '../../hooks/queries.js'
import { detailToMessage } from '../../lib/errorDetail.js'
import { useProviderDisconnect } from './ProviderConnection.jsx'

const CHECK_BODY = JSON.stringify({ contents: [{ role: 'user', parts: [{ text: 'hi' }] }] })

/**
 * The two ways to connect Gemini, matching providers.GEMINI_CONNECTIONS: the
 * vault key each one is saved under, the only paths it may be used on, and one
 * free request Google answers only for a valid key.
 */
export const GEMINI_CONNECTIONS = [
  {
    id: 'ai_studio', key: 'gemini', label: 'AI Studio key', detail: 'Google AI Studio',
    link: 'https://aistudio.google.com/app/apikey', linkLabel: 'Open Google AI Studio',
    host: 'generativelanguage.googleapis.com', paths: ['/v1beta/models', '/v1beta/models/*'],
    check: { destination: 'generativelanguage.googleapis.com/v1beta/models?pageSize=1' },
  },
  {
    id: 'agent_platform', key: 'gemini-agent-platform', label: 'Agent Platform key',
    detail: 'Google Cloud project · Gemini Enterprise Agent Platform',
    link: 'https://console.cloud.google.com/apis/credentials', linkLabel: 'Open Google Cloud credentials',
    host: 'aiplatform.googleapis.com', paths: ['/v1/publishers/google/models', '/v1/publishers/google/models/*'],
    check: {
      destination: 'aiplatform.googleapis.com/v1/publishers/google/models/gemini-3.8-flash:countTokens',
      init: { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: CHECK_BODY },
    },
  },
]

/** The vault record: kept from agents, sent in Google's own header, only to Gemini's model paths. */
export function geminiKeyRecord(connection, value) {
  return {
    value,
    inject: { type: 'header', name: 'x-goog-api-key' },
    grants: { agents: false, sites: [{ host: connection.host, methods: ['GET', 'POST'], paths: connection.paths }] },
  }
}

/** True when Google accepts the key, false when it refuses it, null when the vault could not ask. */
async function keyWorks(connection) {
  const response = await api.keys.use(connection.key, connection.check.destination, connection.check.init)
  if (response.headers.get('X-Vault-Denied')) return null
  return response.ok
}

/** Save and check one connection's key; the other connection's key is removed only once this one works. */
async function connectWith(connection, value) {
  const saved = await api.keys.put(connection.key, geminiKeyRecord(connection, value))
  if (!saved.ok) {
    const data = await saved.json().catch(() => ({}))
    throw new Error(detailToMessage(data.detail, 'Could not save the key.'))
  }
  const works = await keyWorks(connection)
  if (works !== true) {
    await api.keys.revoke(connection.key)
    throw new Error(works === null
      ? 'Möbius could not check the key with Google. Please try again.'
      : 'Google did not accept this key. Check it and try again.')
  }
  const others = GEMINI_CONNECTIONS.filter(other => other.id !== connection.id)
  await Promise.all(others.map(other => api.keys.revoke(other.key).catch(() => null)))
}

function KeyForm({ connection, onConnected }) {
  const queryClient = useQueryClient()
  const [value, setValue] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  async function submit(event) {
    event.preventDefault()
    const key = value.trim()
    if (!key) return
    setSaving(true)
    setError('')
    try {
      await connectWith(connection, key)
      setValue('')
      void authQueries.provider.statuses.invalidate(queryClient)
      void modelQueries.registry.invalidate(queryClient)
      onConnected?.()
    } catch (failure) {
      setError(failure.message || 'Could not connect. Please try again.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="pa__form gemini-connection__form" onSubmit={submit}>
      <a className="pa__btn pa__btn--sm pa__external-link" href={connection.link} target="_blank" rel="noopener noreferrer">
        {connection.linkLabel}
        <ExternalLink width={14} height={14} aria-hidden="true" focusable="false" />
      </a>
      <label className="pa__field">
        <span className="pa__input-label">{connection.label.replace('key', 'API key')}</span>
        <input
          className="pa__input" type="password" autoComplete="off" spellCheck={false} enterKeyHint="go"
          value={value} onChange={(event) => setValue(event.target.value)} disabled={saving}
        />
      </label>
      <button type="submit" className="pa__btn" disabled={saving || !value.trim()}>
        {saving ? 'Checking key…' : 'Save and connect'}
      </button>
      {error && <p className="pa__error" role="alert">{error}</p>}
    </form>
  )
}

function rowAction(connected, active) {
  if (connected) return 'Reconnect'
  return active ? 'Use instead' : 'Connect'
}

/**
 * Google allows no subscription sign-in for third-party agents, so Gemini
 * connects with a key: from AI Studio, or from a Google Cloud project on the
 * Agent Platform. One is in use at a time and the row in use says so.
 */
export default function GeminiConnection({ active, onConnected, onDisconnected }) {
  const [open, setOpen] = useState(null)
  const [confirming, setConfirming] = useState(false)
  const { disconnect, busy, error } = useProviderDisconnect('gemini', onDisconnected)

  const connected = (connection) => connection.id === active
  return (
    <div className="gemini-connection">
      {!active && (
        <p className="pa__muted">
          Google lets third-party apps use Gemini with an API key, not a Google AI Pro or Ultra
          sign-in. Keys are saved in your key vault and sent only to Google.
        </p>
      )}
      {GEMINI_CONNECTIONS.map(connection => (
        <div key={connection.id} className="gemini-connection__row">
          <div className="gemini-connection__head">
            <div>
              <span className="gemini-connection__label">
                {connection.label}
                {connected(connection) && <span className="pa__success gemini-connection__state">Connected</span>}
              </span>
              <span className="pa__muted gemini-connection__detail">{connection.detail}</span>
            </div>
            <div className="provider-connection__actions">
              <button
                type="button" className="settings__btn settings__btn--sm settings__btn--outline"
                aria-expanded={open === connection.id}
                onClick={() => setOpen(open === connection.id ? null : connection.id)}
              >
                {rowAction(connected(connection), Boolean(active))}
              </button>
              {connected(connection) && (
                <button type="button" className="settings__btn settings__btn--sm settings__btn--danger" onClick={() => setConfirming(true)}>
                  Disconnect…
                </button>
              )}
            </div>
          </div>
          {open === connection.id && (
            <KeyForm connection={connection} onConnected={() => { setOpen(null); onConnected?.() }} />
          )}
        </div>
      ))}
      {confirming && (
        <div className="provider-connection" aria-busy={busy}>
          <p>Disconnect Google Gemini from this Möbius? Its saved key is removed.</p>
          <div className="provider-connection__actions">
            <button type="button" className="settings__btn settings__btn--sm settings__btn--outline" disabled={busy} onClick={() => setConfirming(false)}>Cancel</button>
            <button
              type="button" className="settings__btn settings__btn--sm settings__btn--danger" disabled={busy}
              onClick={async () => { if (await disconnect()) setConfirming(false) }}
            >
              {busy ? 'Disconnecting…' : 'Disconnect'}
            </button>
          </div>
          {error && <p className="pa__error" role="alert">{error}</p>}
        </div>
      )}
    </div>
  )
}
