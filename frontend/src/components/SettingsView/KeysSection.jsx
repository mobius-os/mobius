/* Settings › Keys: the overview list, app requests, and each key's own page. */
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { ChevronRight, Key, Plus, X } from '@openai/apps-sdk-ui/components/Icon'
import { Switch } from '@openai/apps-sdk-ui/components/Switch'
import { api, jsonOrThrow } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import './KeysSection.css'

const KEYS_QUERY = ['settings', 'keys']
const NAME_PATTERN = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/
const DEFAULT_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
const BEARER = { type: 'header', name: 'Authorization', prefix: 'Bearer ' }

function useKeys() {
  return useQuery({
    queryKey: KEYS_QUERY,
    queryFn: async ({ signal }) => jsonOrThrow(await api.keys.list({ signal }), 'Keys'),
    staleTime: 0,
  })
}

/** Installed apps by numeric id: names and slugs can be reused. */
function useAppNames() {
  const appsQuery = appQueries.list.useQuery()
  const apps = (appsQuery.data || [])
    .filter(app => Number.isInteger(app.id))
    .map(app => ({ id: app.id, name: app.name || app.slug || `App ${app.id}` }))
    .sort((a, b) => a.name.localeCompare(b.name))
  return { apps, names: Object.fromEntries(apps.map(app => [app.id, app.name])) }
}

function appLabel(names, id) {
  return names[id] || `App ${id}`
}

function relativeTime(iso) {
  if (!iso) return ''
  const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000)
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`
  return `${Math.round(seconds / 86400)} d ago`
}

function usersLine(key, names) {
  const users = (key.grants?.apps || []).map(id => appLabel(names, id))
  if (key.grants?.agents) users.push('agents')
  return users.length ? users.join(', ') : 'Not shared yet'
}

function siteLine(site) {
  const paths = site.paths.length === 1 && site.paths[0] === '/*' ? '' : ` ${site.paths.join(', ')}`
  return `${site.host}${paths}`
}

function sentAs(inject) {
  if (!inject) return 'Authorization: Bearer …'
  return inject.type === 'query'
    ? `?${inject.name}=…`
    : `${inject.name}: ${inject.prefix || ''}…`
}

/** One PUT carrying the key's current record with ``changes`` applied. */
function recordFor(key, changes = {}) {
  return {
    inject: changes.inject || key.inject,
    grants: { ...key.grants, ...(changes.grants || {}) },
    ...(changes.value ? { value: changes.value } : {}),
  }
}

// -- overview ----------------------------------------------------------------

export default function KeysSection({ focusRef, attention = false, onOpenKey, onAddKey }) {
  const keysQuery = useKeys()
  const { names } = useAppNames()
  const queryClient = useQueryClient()
  const data = keysQuery.data
  const keys = data?.keys || []
  const pending = (data?.requests || []).filter(request => request.status === 'pending')
  const denied = (data?.requests || []).filter(request => request.status === 'denied')
  const refresh = () => queryClient.invalidateQueries({ queryKey: KEYS_QUERY })

  return (
    <section
      className={`settings__section settings-keys${attention ? ' settings-setup-target' : ''}`}
      aria-labelledby="settings-keys-title"
      id="settings-keys"
      ref={focusRef}
      tabIndex={-1}
    >
      <div className="settings-keys__head">
        <div>
          <h2 className="settings__section-title settings-keys__title" id="settings-keys-title">Keys</h2>
          <p className="settings__subtext settings__subtext--tight">
            Apps borrow a key without ever seeing it. You and your agents can read them.
          </p>
        </div>
        {data?.available && (
          <button type="button" className="settings__btn settings__btn--outline settings__btn--sm settings-keys__add" onClick={onAddKey}>
            <Plus width={15} height={15} aria-hidden="true" /> Add
          </button>
        )}
      </div>

      {keysQuery.error && <Alert color="danger" variant="soft" description={keysQuery.error.message} />}
      {data && !data.available && (
        <Alert color="warning" variant="soft" description="The key vault isn't running. It starts with Möbius; a restart usually brings it back." />
      )}

      {pending.map(request => (
        <KeyRequest key={request.id} request={request} names={names} onAnswered={refresh} />
      ))}

      {data?.available && (
        <div className="settings-keys__list">
          {keys.length === 0 && <p className="settings-keys__empty">No keys yet.</p>}
          {keys.map(key => (
            <button key={key.name} type="button" className="settings-keys__row" onClick={() => onOpenKey(key.name)}>
              <span className="settings__account-icon settings-keys__icon" aria-hidden="true"><Key width={17} height={17} /></span>
              <span className="settings-keys__row-copy">
                <strong>{key.name}</strong>
                <small>
                  {usersLine(key, names)}
                  {key.last_used ? ` · used ${relativeTime(key.last_used)}` : ''}
                </small>
              </span>
              <ChevronRight className="settings-keys__chevron" width={18} height={18} aria-hidden="true" />
            </button>
          ))}
          {denied.map(request => (
            <DeniedRequest key={request.id} request={request} names={names} onAnswered={refresh} />
          ))}
        </div>
      )}
    </section>
  )
}

function requestAppName(request, names) {
  return request.app_name || appLabel(names, request.app)
}

function KeyRequest({ request, names, onAnswered }) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const isNew = !request.key_exists
  const appName = requestAppName(request, names)

  async function answer(kind) {
    setBusy(kind)
    setError('')
    try {
      const response = kind === 'approve'
        ? await api.keys.approve(request.id, isNew ? { value } : {})
        : await api.keys.deny(request.id)
      await jsonOrThrow(response, kind === 'approve' ? 'Allow' : 'Deny')
      onAnswered()
    } catch (err) {
      setError(err.message)
      setBusy('')
    }
  }

  return (
    <div className="settings-keys__request" role="group" aria-label={`${appName} asks to use ${request.key}`}>
      <p className="settings-keys__request-lead">
        <strong>{appName}</strong> wants to use {isNew ? 'a new key' : 'your key'} <strong>{request.key}</strong>
      </p>
      {request.reason && <p className="settings-keys__muted">{request.reason}</p>}
      {request.sites.length > 0 && (
        <p className="settings-keys__muted">On {request.sites.map(siteLine).join(', ')}</p>
      )}
      {isNew && (
        <input
          className="settings-keys__input"
          type="password"
          value={value}
          onChange={e => setValue(e.target.value)}
          placeholder="Paste the key"
          aria-label="Key value"
          autoComplete="new-password"
          spellCheck={false}
        />
      )}
      {error && <Alert color="danger" variant="soft" description={error} />}
      <div className="settings-keys__actions">
        <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => answer('deny')} disabled={Boolean(busy)}>
          Deny
        </button>
        <button type="button" className="settings__btn settings__btn--sm" onClick={() => answer('approve')} disabled={Boolean(busy) || (isNew && !value)}>
          {busy === 'approve' ? 'Allowing…' : 'Allow'}
        </button>
      </div>
    </div>
  )
}

function DeniedRequest({ request, names, onAnswered }) {
  const [busy, setBusy] = useState(false)
  async function forget() {
    setBusy(true)
    try {
      await jsonOrThrow(await api.keys.forgetRequest(request.id), 'Forget')
      onAnswered()
    } catch {
      setBusy(false)
    }
  }
  return (
    <div className="settings-keys__denied">
      <span>{requestAppName(request, names)} was denied {request.key}</span>
      <button type="button" className="settings-keys__link" onClick={forget} disabled={busy}>Let it ask again</button>
    </div>
  )
}

// -- one key's page ----------------------------------------------------------

/** A key's own page; ``name`` '' is the new-key form. */
export function KeyPage({ name, onOpenKey, onClose }) {
  const keysQuery = useKeys()
  if (name === '') return <NewKey onCreated={onOpenKey} />
  const key = (keysQuery.data?.keys || []).find(item => item.name === name)
  if (keysQuery.isPending) return null
  if (!key) {
    return (
      <div className="settings-keys-page">
        <p className="settings-keys__muted">This key no longer exists.</p>
        <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={onClose}>Back to Settings</button>
      </div>
    )
  }
  return <KeyDetail key={key.name} keyRecord={key} onDeleted={onClose} />
}

function useSaveKey(name) {
  const queryClient = useQueryClient()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function save(record) {
    setBusy(true)
    setError('')
    try {
      await jsonOrThrow(await api.keys.put(name, record), 'Save key')
      await queryClient.invalidateQueries({ queryKey: KEYS_QUERY })
      return true
    } catch (err) {
      setError(err.message)
      return false
    } finally {
      setBusy(false)
    }
  }
  return { save, error, busy }
}

function KeyDetail({ keyRecord: key, onDeleted }) {
  const { apps, names } = useAppNames()
  const { save, error, busy } = useSaveKey(key.name)
  const grantedApps = key.grants?.apps || []
  const ungranted = apps.filter(app => !grantedApps.includes(app.id))
  const setGrants = grants => save(recordFor(key, { grants }))

  return (
    <div className="settings-keys-page">
      {error && <Alert color="danger" variant="soft" description={error} />}

      <section className="settings__section settings-keys-card" aria-label="Value">
        <ValueRow keyRecord={key} save={save} busy={busy} />
      </section>

      <h2 className="settings__section-title settings-keys-page__heading">Who can use it</h2>
      <section className="settings__section settings-keys-card">
        <div className="settings-keys-card__row">
          <span className="settings-keys-card__copy">
            <strong>Agents</strong>
            <small>They borrow it without seeing it</small>
          </span>
          <Switch
            className="settings-keys-switch"
            checked={Boolean(key.grants?.agents)}
            onCheckedChange={checked => setGrants({ agents: checked })}
            disabled={busy}
            aria-label="Agents can use this key"
          />
        </div>
        {grantedApps.map(id => (
          <div className="settings-keys-card__row" key={id}>
            <span className="settings-keys-card__copy"><strong>{appLabel(names, id)}</strong></span>
            <button
              type="button"
              className="settings-keys__icon-btn"
              aria-label={`Stop ${appLabel(names, id)} using this key`}
              onClick={() => setGrants({ apps: grantedApps.filter(other => other !== id) })}
              disabled={busy}
            >
              <X width={16} height={16} aria-hidden="true" />
            </button>
          </div>
        ))}
        {ungranted.length > 0 && (
          <label className="settings-keys-card__row settings-keys-card__row--add">
            <Plus width={16} height={16} aria-hidden="true" />
            <select
              className="settings-keys__select"
              value=""
              onChange={e => e.target.value && setGrants({ apps: [...grantedApps, Number(e.target.value)] })}
              disabled={busy}
              aria-label="Let an app use this key"
            >
              <option value="">Let an app use it…</option>
              {ungranted.map(app => <option key={app.id} value={app.id}>{app.name}</option>)}
            </select>
          </label>
        )}
      </section>

      <h2 className="settings__section-title settings-keys-page__heading">Sites</h2>
      <section className="settings__section settings-keys-card">
        <SitesRows keyRecord={key} setGrants={setGrants} busy={busy} />
      </section>

      <SentAs keyRecord={key} save={save} busy={busy} />

      <h2 className="settings__section-title settings-keys-page__heading">Recent use</h2>
      <section className="settings__section settings-keys-card">
        <KeyUsage name={key.name} names={names} />
      </section>

      <DeleteKey name={key.name} onDeleted={onDeleted} />
    </div>
  )
}

function ValueRow({ keyRecord: key, save, busy }) {
  const [shown, setShown] = useState(null)
  const [replacing, setReplacing] = useState(false)
  const [next, setNext] = useState('')
  const [error, setError] = useState('')

  async function show() {
    setError('')
    try {
      setShown((await jsonOrThrow(await api.keys.reveal(key.name), 'Show key')).value)
    } catch (err) {
      setError(err.message)
    }
  }

  async function replace(event) {
    event.preventDefault()
    if (await save(recordFor(key, { value: next }))) {
      setNext('')
      setReplacing(false)
      setShown(null)
    }
  }

  return (
    <>
      <div className="settings-keys-card__row">
        <span className="settings-keys-card__copy">
          <strong>Value</strong>
          {shown === null
            ? <small aria-label="Hidden">••••••••••••</small>
            : <code className="settings-keys__value">{shown}</code>}
        </span>
        <span className="settings-keys-card__end">
          <button type="button" className="settings-keys__link" onClick={() => (shown === null ? show() : setShown(null))}>
            {shown === null ? 'Show' : 'Hide'}
          </button>
          {!replacing && (
            <button type="button" className="settings-keys__link" onClick={() => setReplacing(true)}>Replace</button>
          )}
        </span>
      </div>
      {error && <small className="settings-keys__error" role="alert">{error}</small>}
      {replacing && (
        <form className="settings-keys-card__form" onSubmit={replace} autoComplete="off">
          <input
            className="settings-keys__input"
            type="password"
            value={next}
            onChange={e => setNext(e.target.value)}
            placeholder="Paste the new key"
            aria-label="New key value"
            autoComplete="new-password"
            spellCheck={false}
            autoFocus
          />
          <div className="settings-keys__actions">
            <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => { setReplacing(false); setNext('') }}>Cancel</button>
            <button type="submit" className="settings__btn settings__btn--sm" disabled={busy || !next}>Save</button>
          </div>
        </form>
      )}
    </>
  )
}

function SitesRows({ keyRecord: key, setGrants, busy }) {
  const sites = key.grants?.sites || []
  const [adding, setAdding] = useState(false)
  const [host, setHost] = useState('')
  const [paths, setPaths] = useState('/*')

  async function add(event) {
    event.preventDefault()
    const site = {
      host: host.trim().toLowerCase(),
      methods: DEFAULT_METHODS,
      paths: paths.split(',').map(path => path.trim()).filter(Boolean),
    }
    if (await setGrants({ sites: [...sites, site] })) {
      setAdding(false)
      setHost('')
      setPaths('/*')
    }
  }

  return (
    <>
      {sites.length === 0 && !adding && (
        <p className="settings-keys-card__row settings-keys__muted">No sites yet: nobody can use this key until you add one.</p>
      )}
      {sites.map((site, index) => (
        <div className="settings-keys-card__row" key={`${site.host}-${index}`}>
          <span className="settings-keys-card__copy">
            <strong>{site.host}</strong>
            <small>{site.paths.join(', ')} · {site.methods.join(', ')}</small>
          </span>
          <button
            type="button"
            className="settings-keys__icon-btn"
            aria-label={`Remove ${site.host}`}
            onClick={() => setGrants({ sites: sites.filter((_, i) => i !== index) })}
            disabled={busy}
          >
            <X width={16} height={16} aria-hidden="true" />
          </button>
        </div>
      ))}
      {adding ? (
        <form className="settings-keys-card__form" onSubmit={add} autoComplete="off">
          <input className="settings-keys__input" value={host} onChange={e => setHost(e.target.value)} placeholder="api.example.com" aria-label="Site" inputMode="url" autoFocus />
          <input className="settings-keys__input" value={paths} onChange={e => setPaths(e.target.value)} placeholder="/v1/*" aria-label="Paths" />
          <div className="settings-keys__actions">
            <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => setAdding(false)}>Cancel</button>
            <button type="submit" className="settings__btn settings__btn--sm" disabled={busy || !host.trim()}>Add site</button>
          </div>
        </form>
      ) : (
        <button type="button" className="settings-keys-card__row settings-keys-card__row--add" onClick={() => setAdding(true)}>
          <Plus width={16} height={16} aria-hidden="true" /> Add a site
        </button>
      )}
    </>
  )
}

function SentAs({ keyRecord: key, save, busy }) {
  const inject = key.inject || BEARER
  const [slot, setSlot] = useState(inject.type)
  const [headerName, setHeaderName] = useState(inject.type === 'header' ? inject.name : 'Authorization')
  const [prefix, setPrefix] = useState(inject.type === 'header' ? (inject.prefix || '') : 'Bearer ')
  const [queryName, setQueryName] = useState(inject.type === 'query' ? inject.name : 'api_key')

  function submit(event) {
    event.preventDefault()
    save(recordFor(key, {
      inject: slot === 'query'
        ? { type: 'query', name: queryName.trim() }
        : { type: 'header', name: headerName.trim(), prefix },
    }))
  }

  return (
    <details className="settings-keys-sent">
      <summary>
        <span>How it's sent</span>
        <code>{sentAs(inject)}</code>
      </summary>
      <form className="settings-keys-card__form" onSubmit={submit} autoComplete="off">
        <div className="settings-keys__segmented" role="radiogroup" aria-label="Sent as">
          {[['header', 'Header'], ['query', 'In the address']].map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={slot === value}
              className={`settings-keys__segment${slot === value ? ' settings-keys__segment--on' : ''}`}
              onClick={() => setSlot(value)}
            >{label}</button>
          ))}
        </div>
        {slot === 'header' ? (
          <>
            <input className="settings-keys__input" value={headerName} onChange={e => setHeaderName(e.target.value)} aria-label="Header name" />
            <input className="settings-keys__input" value={prefix} onChange={e => setPrefix(e.target.value)} placeholder="Nothing before the key" aria-label="Before the key" />
          </>
        ) : (
          <input className="settings-keys__input" value={queryName} onChange={e => setQueryName(e.target.value)} aria-label="Parameter name" />
        )}
        <div className="settings-keys__actions">
          <button type="submit" className="settings__btn settings__btn--sm" disabled={busy}>Save</button>
        </div>
      </form>
    </details>
  )
}

function usageText(entry, names) {
  const [kind, id] = entry.caller.split(':')
  const who = kind === 'app' ? appLabel(names, Number(id))
    : kind === 'agent' ? 'An agent'
    : kind === 'platform' ? 'Möbius'
    : kind === 'owner' ? 'You' : entry.caller
  if (entry.outcome === 'used') return `${who} used it`
  if (entry.outcome === 'revealed') return `${who} read the value`
  if (entry.outcome === 'requested') return `${who} asked to use it`
  if (entry.outcome === 'failed') return `${who} couldn't reach the site`
  if (entry.outcome === 'denied') return entry.host === '-' ? `${who} tried to read the value` : `${who} was refused`
  if (entry.outcome.startsWith('revoked app:')) return `${appLabel(names, Number(entry.outcome.split(':')[1]))} lost access (app deleted)`
  return null
}

function KeyUsage({ name, names }) {
  const usageQuery = useQuery({
    queryKey: [...KEYS_QUERY, 'usage', name],
    queryFn: async ({ signal }) => jsonOrThrow(await api.keys.usage(name), 'Key use'),
    staleTime: 0,
  })
  const entries = (usageQuery.data?.usage || [])
    .map(entry => ({ entry, text: usageText(entry, names) }))
    .filter(item => item.text)
  if (entries.length === 0) return <p className="settings-keys-card__row settings-keys__muted">Nothing yet.</p>
  return entries.map(({ entry, text }, index) => (
    <div className="settings-keys-card__row" key={`${entry.ts}-${index}`}>
      <span className="settings-keys-card__copy">
        <span className={entry.outcome === 'denied' || entry.outcome === 'failed' ? 'settings-keys__refused' : undefined}>{text}</span>
        <small>
          {entry.host !== '-' ? `${entry.host}${entry.path} · ` : ''}
          {relativeTime(entry.ts)}
        </small>
      </span>
    </div>
  ))
}

function DeleteKey({ name, onDeleted }) {
  const queryClient = useQueryClient()
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function remove() {
    setBusy(true)
    setError('')
    try {
      await jsonOrThrow(await api.keys.revoke(name), 'Delete key')
      await queryClient.invalidateQueries({ queryKey: KEYS_QUERY })
      onDeleted()
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }
  return (
    <div className="settings-keys-delete">
      {error && <Alert color="danger" variant="soft" description={error} />}
      {confirm ? (
        <div className="settings-keys__actions">
          <span className="settings-keys__muted">Delete this key for good? Apps and agents lose it.</span>
          <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => setConfirm(false)} disabled={busy}>Keep</button>
          <button type="button" className="settings__btn settings__btn--sm settings-keys__danger" onClick={remove} disabled={busy}>Delete</button>
        </div>
      ) : (
        <button type="button" className="settings__btn settings__btn--outline settings-keys__danger-outline" onClick={() => setConfirm(true)}>Delete key</button>
      )}
    </div>
  )
}

function NewKey({ onCreated }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [value, setValue] = useState('')
  const [host, setHost] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const nameValid = NAME_PATTERN.test(name)

  async function create(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const sites = host.trim()
        ? [{ host: host.trim().toLowerCase(), methods: DEFAULT_METHODS, paths: ['/*'] }]
        : []
      await jsonOrThrow(await api.keys.put(name, {
        value, inject: BEARER, grants: { apps: [], agents: true, sites },
      }), 'Add key')
      await queryClient.invalidateQueries({ queryKey: KEYS_QUERY })
      onCreated(name)
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <form className="settings-keys-page" onSubmit={create} autoComplete="off">
      <section className="settings__section settings-keys-card">
        <label className="settings-keys-card__field">
          <span>Name</span>
          <input className="settings-keys__input" value={name} onChange={e => setName(e.target.value)} placeholder="openweather" aria-invalid={name !== '' && !nameValid} required autoFocus />
        </label>
        <label className="settings-keys-card__field">
          <span>Key</span>
          <input className="settings-keys__input" type="password" value={value} onChange={e => setValue(e.target.value)} placeholder="Paste the key" autoComplete="new-password" spellCheck={false} required />
        </label>
        <label className="settings-keys-card__field">
          <span>Site it's for</span>
          <input className="settings-keys__input" value={host} onChange={e => setHost(e.target.value)} placeholder="api.example.com" inputMode="url" />
        </label>
      </section>
      <p className="settings-keys__muted settings-keys-page__note">
        Sent as “Authorization: Bearer …” and usable by your agents. You can change both on the key's page.
      </p>
      {error && <Alert color="danger" variant="soft" description={error} />}
      <div className="settings-keys__actions">
        <button type="submit" className="settings__btn" disabled={busy || !nameValid || !value}>
          {busy ? 'Adding…' : 'Add key'}
        </button>
      </div>
    </form>
  )
}
