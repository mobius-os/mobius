import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ExternalLink } from '@openai/apps-sdk-ui/components/Icon'
import { api } from '../../api/client.js'
import { authQueries, modelQueries } from '../../hooks/queries.js'
import { detailToMessage } from '../../lib/errorDetail.js'
import { useProviderDisconnect } from './ProviderConnection.jsx'
import GoogleCloudSignIn from './GoogleCloudSignIn.jsx'

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
  await Promise.all([
    ...others.map(other => api.keys.revoke(other.key).catch(() => null)),
    api.auth.provider.geminiGoogleCloud.forget().catch(() => null),
  ])
}

const METHOD_LABELS = {
  ai_studio: 'AI Studio key',
  agent_platform: 'Agent Platform key',
  google_cloud: 'Google Cloud sign-in',
}
const OUTLINE = 'settings__btn settings__btn--sm settings__btn--outline'
const PRIMARY = 'settings__btn settings__btn--sm'
const DANGER = 'settings__btn settings__btn--sm settings__btn--danger'

/** The Connection box's label and value rows for a connected Gemini, as Claude's shows its version. */
export function geminiConnectionDetails(status) {
  const kind = status?.authenticated ? status.connection : null
  if (!METHOD_LABELS[kind]) return []
  const rows = [{ label: 'Connected with', value: METHOD_LABELS[kind] }]
  if (kind !== 'google_cloud') return [...rows, { label: 'Key', value: 'Saved in Settings › Keys' }]
  if (!status.project) return rows
  const { id, name } = status.project
  return [...rows, {
    label: 'Project',
    value: (
      <span className="gemini-connection__project">
        {name !== id && <span>{name}</span>}
        <span className="gemini-connection__project-id">{id}</span>
      </span>
    ),
  }]
}

const DISCONNECT_COPY = {
  google_cloud: 'Möbius’s access is also removed from your Google account; your Cloud project and billing don’t change.',
  key: 'The saved key is deleted from this Möbius; it keeps working in Google’s console until you delete it there.',
}

function KeyForm({ connection, switching, onConnected, onCancel }) {
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
      <label className="pa__field">
        <span className="pa__input-label">{connection.label.replace('key', 'API key')}</span>
        <input
          className="pa__input" type="password" autoComplete="off" spellCheck={false} enterKeyHint="go"
          value={value} onChange={(event) => setValue(event.target.value)} disabled={saving}
        />
      </label>
      <p className="pa__muted">
        {switching ? 'When Google accepts the key, it replaces the current connection. ' : ''}
        <a className="pa__external-link" href={connection.link} target="_blank" rel="noopener noreferrer">
          {connection.linkLabel}
          <ExternalLink width={12} height={12} aria-hidden="true" focusable="false" />
        </a>
      </p>
      <div className="provider-connection__actions">
        <button type="submit" className={PRIMARY} disabled={saving || !value.trim()}>
          {saving ? 'Checking key…' : switching ? 'Save and switch' : 'Save and connect'}
        </button>
        <button type="button" className={OUTLINE} disabled={saving} onClick={onCancel}>Cancel</button>
      </div>
      {error && <p className="pa__error" role="alert">{error}</p>}
    </form>
  )
}

function Chevron() {
  return (
    <svg className="gemini-connection__chevron" width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
      <path d="M4.5 2.5 8 6l-3.5 3.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/** An expandable row: a title and detail that open its content in place. */
function Disclosure({ title, detail, open, onToggle, children }) {
  return (
    <div className={`gemini-disclosure${open ? ' gemini-disclosure--open' : ''}`}>
      <button type="button" className="gemini-disclosure__head" aria-expanded={open} onClick={onToggle}>
        <span>
          <span className="gemini-connection__label">{title}</span>
          {detail && <span className="pa__muted gemini-connection__detail">{detail}</span>}
        </span>
        <Chevron />
      </button>
      {open && <div className="gemini-disclosure__body">{children}</div>}
    </div>
  )
}

/** The two keys as expandable rows; one opens at a time, the one in use is left out. */
function KeyChoices({ active, done }) {
  const [open, setOpen] = useState(null)
  return (
    <div className="gemini-disclosures">
      {GEMINI_CONNECTIONS.filter(connection => connection.id !== active).map(connection => (
        <Disclosure
          key={connection.id} title={connection.label} detail={connection.detail}
          open={open === connection.id} onToggle={() => setOpen(open === connection.id ? null : connection.id)}
        >
          <KeyForm connection={connection} switching={Boolean(active)} onConnected={done} onCancel={() => setOpen(null)} />
        </Disclosure>
      ))}
    </div>
  )
}

/** Everything but the connection in use: the Google sign-in and the keys, each expandable. */
function OtherMethods({ active, done }) {
  const [open, setOpen] = useState(null)
  const toggle = (id) => setOpen(open === id ? null : id)
  return (
    <>
      {active !== 'google_cloud' && (
        <Disclosure
          title="Sign in with Google" detail="Google Cloud (Agent Platform), billed to a project you choose"
          open={open === 'google'} onToggle={() => toggle('google')}
        >
          <GoogleCloudSignIn onConnected={done} onCancel={() => setOpen(null)} />
        </Disclosure>
      )}
      <Disclosure title="Connect with an API key" detail="AI Studio key or Agent Platform key" open={open === 'keys'} onToggle={() => toggle('keys')}>
        <KeyChoices active={active} done={done} />
      </Disclosure>
    </>
  )
}

function DisconnectConfirm({ active, onCancel, onDisconnected }) {
  const { disconnect, busy, error } = useProviderDisconnect('gemini', onDisconnected)
  return (
    <div className="provider-connection" aria-busy={busy}>
      <p>Disconnect Google Gemini from this Möbius?</p>
      <p className="pa__muted">
        Your chats and settings stay. New Gemini tasks will need a connection.{' '}
        {DISCONNECT_COPY[active === 'google_cloud' ? 'google_cloud' : 'key']}
      </p>
      <div className="provider-connection__actions">
        <button type="button" className={OUTLINE} disabled={busy} onClick={onCancel}>Cancel</button>
        <button type="button" className={DANGER} disabled={busy} onClick={disconnect}>
          {busy ? 'Disconnecting…' : 'Disconnect'}
        </button>
      </div>
      {error && <p className="pa__error" role="alert">{error}</p>}
    </div>
  )
}

function Reconnect({ active, done, onCancel }) {
  if (active === 'google_cloud') return <GoogleCloudSignIn onConnected={done} onCancel={onCancel} />
  const connection = GEMINI_CONNECTIONS.find(row => row.id === active)
  return <KeyForm connection={connection} onConnected={done} onCancel={onCancel} />
}

function Connected({ active, onConnected, onDisconnected }) {
  const [view, setView] = useState('manage')
  const done = () => { setView('manage'); onConnected?.() }
  if (view === 'reconnect') return <Reconnect active={active} done={done} onCancel={() => setView('manage')} />
  if (view === 'disconnect') {
    return <DisconnectConfirm active={active} onCancel={() => setView('manage')} onDisconnected={onDisconnected} />
  }
  return (
    <div className="gemini-connection">
      <div className="provider-connection__actions">
        <button type="button" className={OUTLINE} onClick={() => setView('reconnect')}>Reconnect</button>
        <button type="button" className={DANGER} onClick={() => setView('disconnect')}>Disconnect…</button>
      </div>
      <button
        type="button" className="gemini-connection__fold" aria-expanded={view === 'others'}
        onClick={() => setView(view === 'others' ? 'manage' : 'others')}
      >
        <span>Other ways to connect</span>
        <Chevron />
      </button>
      {view === 'others' && <OtherMethods active={active} done={done} />}
    </div>
  )
}

function NotConnected({ onConnected }) {
  const [keysOpen, setKeysOpen] = useState(false)
  return (
    <div className="gemini-connection">
      <GoogleCloudSignIn onConnected={onConnected} />
      <Disclosure
        title="Connect with an API key instead" detail="AI Studio key or Agent Platform key"
        open={keysOpen} onToggle={() => setKeysOpen(!keysOpen)}
      >
        <KeyChoices active={null} done={onConnected} />
      </Disclosure>
    </div>
  )
}

/**
 * Gemini's Connection box, laid out like Claude's and Codex's. Google allows
 * no subscription sign-in for third-party agents, so the main way in is a
 * Google Cloud sign-in, with an AI Studio or Agent Platform key as the
 * alternatives. Once connected, the box shows Reconnect and Disconnect… and
 * folds the other ways to connect away.
 */
export default function GeminiConnection({ active, onConnected, onDisconnected }) {
  if (!active) return <NotConnected onConnected={onConnected} />
  return <Connected key={active} active={active} onConnected={onConnected} onDisconnected={onDisconnected} />
}
