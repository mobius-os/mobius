/* Settings › Keys: the overview list, app and agent requests, and each key's own page. */
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Key, Plus, X } from '@openai/apps-sdk-ui/components/Icon'
import { Switch } from '@openai/apps-sdk-ui/components/Switch'
import { api, jsonOrThrow } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import {
  distinctAppNames, appLabel, relativeTime, savedByApp, replaceWarning, siteLine, sentAs, sentToLine, usedByLine, lastUseByCaller, usedByRows, newKeyName, requestFormKey, shownRequests, requestAppName, requestWording, usageSentence,
  REQUESTS_SHOWN,
} from '../../lib/keysSettingsText.js'
import './KeysSection.css'

const KEYS_QUERY = ['settings', 'keys']
const DEFAULT_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
const BEARER = { type: 'header', name: 'Authorization', prefix: 'Bearer ' }
// Enough history to know when each app last used the key; the page lists fewer.
const USAGE_FETCHED = 50
const ACTIVITY_SHOWN = 8

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
  const apps = distinctAppNames((appsQuery.data || [])
    .filter(app => Number.isInteger(app.id))
    // The installation, because a removed app's id can be reused.
    .map(app => ({ id: app.id, name: app.name || app.slug || `App ${app.id}`, installation: app.storage_generation })))
    .sort((a, b) => a.name.localeCompare(b.name))
  return { apps, names: Object.fromEntries(apps.map(app => [app.id, app.name])) }
}

/** One PUT carrying the key's current record with ``changes`` applied. */
function recordFor(key, changes = {}) {
  return {
    inject: changes.inject || key.inject,
    grants: { ...key.grants, ...(changes.grants || {}) },
    // The vault refuses a save made from a stale copy of the key.
    revision: key.revision,
    ...(changes.value ? { value: changes.value } : {}),
    ...(changes.installations ? { installations: changes.installations } : {}),
  }
}

/** "How it's sent" as an editable draft, and back. */
function draftFromInject(inject = BEARER) {
  return {
    slot: inject.type,
    headerName: inject.type === 'header' ? inject.name : 'Authorization',
    prefix: inject.type === 'header' ? (inject.prefix || '') : 'Bearer ',
    queryName: inject.type === 'query' ? inject.name : 'api_key',
  }
}

function injectFromDraft(draft) {
  return draft.slot === 'query'
    ? { type: 'query', name: draft.queryName.trim() }
    : { type: 'header', name: draft.headerName.trim(), prefix: draft.prefix }
}

// -- overview ----------------------------------------------------------------

export default function KeysSection({ focusRef, attention = false, onOpenKey, onAddKey }) {
  const keysQuery = useKeys()
  const { names } = useAppNames()
  const queryClient = useQueryClient()
  const [showAll, setShowAll] = useState(false)
  const data = keysQuery.data
  const keys = data?.keys || []
  const pending = (data?.requests || []).filter(request => request.status === 'pending')
  // A switched-off app asking for more websites stays off after Allow; the card says so.
  const pausedApp = request => Boolean((keys.find(key => key.name === request.key)?.grants?.apps || [])
    .find(grant => grant.id === request.app)?.paused)
  const denied = (data?.requests || []).filter(request => request.status === 'denied')
  const refresh = () => queryClient.invalidateQueries({ queryKey: KEYS_QUERY })

  return (
    <section
      className={`settings-group settings-keys${attention ? ' settings-setup-target' : ''}`}
      aria-labelledby="settings-keys-title"
      id="settings-keys"
      ref={focusRef}
      tabIndex={-1}
    >
      <div className="settings-group__head">
        <div className="settings-group__heading">
          <h2 className="settings-group__title" id="settings-keys-title">Keys</h2>
          <p className="settings-group__desc">
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

      {shownRequests(pending, showAll).map(request => (
        <KeyRequest
          key={requestFormKey(request)}
          request={{ ...request, app_paused: pausedApp(request) }}
          names={names}
          onAnswered={refresh}
        />
      ))}
      {!showAll && pending.length > REQUESTS_SHOWN && (
        <button type="button" className="settings-keys__link settings-keys__more" onClick={() => setShowAll(true)}>
          Show {pending.length - REQUESTS_SHOWN} more {pending.length - REQUESTS_SHOWN === 1 ? 'request' : 'requests'}
        </button>
      )}

      {data?.available && (
        <div className="settings-list">
          {keys.length === 0 && <p className="settings-keys__empty">No keys yet.</p>}
          {keys.map(key => (
            // Opens its own page, so no arrow: arrows in Settings mean "expands here".
            <button key={key.name} type="button" className="settings-item" onClick={() => onOpenKey(key.name)}>
              <span className="settings-item__icon" aria-hidden="true"><Key width={18} height={18} /></span>
              <span className="settings-item__body">
                <span className="settings-item__title">{key.name}</span>
                <span className="settings-item__sub settings-item__sub--wrap">
                  {savedByApp(key, names) ? `Saved by ${savedByApp(key, names)} · ` : ''}
                  {usedByLine(key, names)}
                  {key.last_used ? ` · used ${relativeTime(key.last_used)}` : ''}
                </span>
              </span>
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

function Lead({ segments }) {
  return segments.map((segment, index) => (
    typeof segment === 'string'
      ? <span key={index}>{segment}</span>
      : <strong key={index}>{segment.text}</strong>
  ))
}

function KeyRequest({ request, names, onAnswered }) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const words = requestWording(request, names)
  // The requester only proposes: Allow grants exactly what this card shows.
  // An app's sites go to that app's own grant; an agent's to the sites for
  // the owner and agents.
  const inject = request.inject || BEARER
  const sites = request.sites || []

  async function answer(kind) {
    setBusy(kind)
    setError('')
    try {
      const response = kind === 'approve'
        ? await api.keys.approve(request.id, {
          ticket: request.ticket, sites, ...(words.needsValue ? { value, inject } : {}),
        })
        : await api.keys.deny(request.id, request.ticket)
      await jsonOrThrow(response, kind === 'approve' ? 'Allow' : 'Not now')
      onAnswered()
    } catch (err) {
      setError(err.message)
      setBusy('')
    }
  }

  const denyButton = (
    <button
      type="button"
      className={`settings__btn settings__btn--sm${words.allowHidden ? '' : ' settings__btn--outline'}`}
      onClick={() => answer('deny')}
      disabled={Boolean(busy)}
    >
      Not now
    </button>
  )
  const allowButton = (label, primary) => (
    <button
      type="button"
      className={`settings__btn settings__btn--sm${primary ? '' : ' settings__btn--outline'}`}
      onClick={() => answer('approve')}
      disabled={Boolean(busy) || (words.needsValue && !value)}
    >
      {busy === 'approve' ? 'Allowing…' : label}
    </button>
  )

  return (
    <div className="settings-keys__request" role="group" aria-label={`${words.who} asks to use ${request.key}`}>
      <p className="settings-keys__request-lead"><Lead segments={words.lead} /></p>
      {request.reason && <p className="settings-keys__muted settings-keys__reason">“{request.reason}”</p>}
      {words.warning && <Alert color="warning" variant="soft" description={words.warning} />}
      <p className="settings-keys__muted">{words.note}</p>
      {words.needsValue && (
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
      <details className="settings-keys__details">
        <summary>Details</summary>
        <ul className="settings-keys__details-list">
          {sites.map((site, index) => <li key={`${site.host}-${index}`}>{siteLine(site)}</li>)}
          {words.needsValue && <li>Sent as <code>{sentAs(inject)}</code></li>}
          <li>Saved as “{request.key}”</li>
        </ul>
      </details>
      {words.allowHidden && (
        <details className="settings-keys__details">
          <summary>Use {words.savedBy}'s key anyway</summary>
          <div className="settings-keys__details-body">
            <p className="settings-keys__muted">
              {words.who}'s requests would be signed with {words.savedBy}'s account, and {words.savedBy} can change that key at any time.
            </p>
            <div className="settings-keys__actions settings-keys__actions--start">
              {allowButton(`Use ${words.savedBy}'s key`, false)}
            </div>
          </div>
        </details>
      )}
      {error && <Alert color="danger" variant="soft" description={error} />}
      <div className="settings-keys__actions">
        {denyButton}
        {!words.allowHidden && allowButton('Allow', true)}
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
      <span>You said no to {request.requester === 'agent' ? 'your agent' : requestAppName(request, names)} using {request.key}</span>
      <button type="button" className="settings-keys__link" onClick={forget} disabled={busy}>Let it ask again</button>
    </div>
  )
}

// -- one key's page ----------------------------------------------------------

/** A key's own page; ``name`` '' is the new-key form. */
export function KeyPage({ name, onOpenKey, onClose }) {
  const keysQuery = useKeys()
  if (name === '') return <NewKey existingNames={(keysQuery.data?.keys || []).map(key => key.name)} onCreated={onOpenKey} />
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
      await queryClient.invalidateQueries({ queryKey: KEYS_QUERY })
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
  const usageQuery = useQuery({
    queryKey: [...KEYS_QUERY, 'usage', key.name],
    queryFn: async () => jsonOrThrow(await api.keys.usage(key.name, USAGE_FETCHED), 'Key use'),
    staleTime: 0,
  })
  const usage = usageQuery.data?.usage || []
  const grantedApps = key.grants?.apps || []
  const grantedIds = grantedApps.map(grant => grant.id)
  const ungranted = apps.filter(app => !grantedIds.includes(app.id))
  const rows = usedByRows(key, names, lastUseByCaller(usage))
  const setGrants = grants => save(recordFor(key, { grants }))
  const updateApp = (id, change) => setGrants({
    apps: grantedApps.map(grant => (grant.id === id ? { ...grant, ...change } : grant)),
  })
  // A new app starts with no sites; its own row under Advanced adds the ones it may reach.
  const grantApp = app => save(recordFor(key, {
    grants: { apps: [...grantedApps, { id: app.id, sites: [] }] },
    installations: { [app.id]: app.installation },
  }))

  return (
    <div className="settings-keys-page">
      <p className="settings-keys__muted settings-keys-page__summary">{sentToLine(key)}</p>
      {error && <Alert color="danger" variant="soft" description={error} />}

      <h2 className="settings-group__title settings-keys-page__heading">Your key</h2>
      <section className="settings-list settings-keys-card" aria-label="Your key">
        <ValueRow keyRecord={key} names={names} save={save} busy={busy} />
      </section>

      <h2 className="settings-group__title settings-keys-page__heading">Used by</h2>
      <section className="settings-list settings-keys-card" aria-label="Used by">
        {rows.apps.map(row => (
          <UsedByRow
            key={row.id}
            row={row}
            busy={busy}
            switchLabel={`${row.label} can use it`}
            onChange={on => updateApp(row.id, { paused: !on })}
          />
        ))}
        <UsedByRow
          row={rows.agents}
          busy={busy}
          switchLabel="Your agents can use it"
          onChange={on => setGrants({ agents: on })}
        />
      </section>

      <h2 className="settings-group__title settings-keys-page__heading">Recent activity</h2>
      <section className="settings-list settings-keys-card" aria-label="Recent activity">
        <RecentActivity usage={usage} names={names} />
      </section>

      <details className="settings-keys-disclosure">
        <summary><span>Advanced</span></summary>
        <div className="settings-keys-disclosure__body">
          <h3 className="settings-keys-disclosure__heading">Apps and their websites</h3>
          <section className="settings-list settings-keys-card">
            {grantedApps.map(grant => (
              <AppGrantRow
                key={grant.id}
                grant={grant}
                label={appLabel(names, grant.id)}
                busy={busy}
                onRemove={() => setGrants({ apps: grantedApps.filter(other => other.id !== grant.id) })}
                onSites={sites => updateApp(grant.id, { sites })}
              />
            ))}
            {ungranted.length > 0 && (
              <label className="settings-keys-card__row settings-keys-card__row--add">
                <Plus width={16} height={16} aria-hidden="true" />
                <select
                  className="settings-keys__select"
                  value=""
                  onChange={e => {
                    const app = ungranted.find(item => item.id === Number(e.target.value))
                    if (app) grantApp(app)
                  }}
                  disabled={busy}
                  aria-label="Let an app use this key"
                >
                  <option value="">Let an app use it…</option>
                  {ungranted.map(app => <option key={app.id} value={app.id}>{app.name}</option>)}
                </select>
              </label>
            )}
            {grantedApps.length === 0 && ungranted.length === 0 && (
              <p className="settings-keys-card__row settings-keys__muted">No apps installed.</p>
            )}
          </section>

          <h3 className="settings-keys-disclosure__heading">Websites for you and your agents</h3>
          <section className="settings-list settings-keys-card">
            <SitesRows
              sites={key.grants?.sites || []}
              onChange={sites => setGrants({ sites })}
              busy={busy}
              empty="None yet: you and your agents can't send this key until you add one, or approve your agent's request."
            />
          </section>
          {key.multiline && (
            <p className="settings-keys__muted settings-keys-page__note">
              This key has several lines, so it can be sent only in the address, never as a header.
            </p>
          )}

          <h3 className="settings-keys-disclosure__heading">How it's sent</h3>
          <SentAs keyRecord={key} save={save} busy={busy} />

          <DeleteKey name={key.name} onDeleted={onDeleted} />
        </div>
      </details>
    </div>
  )
}

/** One "Used by" row: initial or agent mark, name, last use, and its switch. */
function UsedByRow({ row, busy, switchLabel, onChange }) {
  return (
    <div className="settings-keys-card__row">
      <span className="settings-item__icon settings-keys-usedby__icon" aria-hidden="true">
        {row.initial || '✦'}
      </span>
      <span className="settings-keys-card__copy">
        <strong>{row.label}</strong>
        <small>{row.sub}</small>
      </span>
      <Switch
        className="settings-keys-switch"
        checked={row.on}
        onCheckedChange={onChange}
        disabled={busy}
        aria-label={switchLabel}
      />
    </div>
  )
}

/** One app allowed to use the key, with the sites only that app may reach. */
function AppGrantRow({ grant, label, busy, onRemove, onSites }) {
  const [open, setOpen] = useState(false)
  const count = grant.sites.length
  return (
    <div className="settings-keys-grant">
      <div className="settings-keys-card__row">
        <button type="button" className="settings-keys-card__copy settings-keys-grant__toggle" onClick={() => setOpen(!open)} aria-expanded={open}>
          <strong>{label}{grant.paused ? ' (switched off)' : ''}</strong>
          <small className={count === 0 ? 'settings-keys__warn' : undefined}>
            {count === 0 ? 'No websites yet' : count === 1 ? siteLine(grant.sites[0]) : `${count} websites`}
          </small>
        </button>
        <button
          type="button"
          className="settings-keys__icon-btn"
          aria-label={`Stop ${label} using this key`}
          onClick={onRemove}
          disabled={busy}
        >
          <X width={16} height={16} aria-hidden="true" />
        </button>
      </div>
      {open && (
        <div className="settings-keys-grant__sites">
          <SitesRows
            sites={grant.sites}
            onChange={onSites}
            busy={busy}
            empty={`No websites yet: ${label} can't send this key until you add one.`}
          />
        </div>
      )}
    </div>
  )
}

/** The value field: one masked line, or a box for keys with several lines. */
function ValueInput({ value, onChange, multiline, placeholder, label, autoFocus }) {
  return multiline ? (
    <textarea
      className="settings-keys__input settings-keys__textarea"
      value={value}
      onChange={e => onChange(e.target.value)}
      placeholder="Paste the whole key, including its first and last lines"
      aria-label={label}
      spellCheck={false}
      autoCapitalize="off"
      autoFocus={autoFocus}
      rows={6}
    />
  ) : (
    <input
      className="settings-keys__input"
      type="password"
      value={value}
      onChange={e => onChange(e.target.value)}
      placeholder={placeholder}
      aria-label={label}
      autoComplete="new-password"
      spellCheck={false}
      autoFocus={autoFocus}
    />
  )
}

function MultilineToggle({ checked, onChange }) {
  return (
    <label className="settings-keys__check">
      <input type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)} />
      <span>Several lines <em>· a certificate or key file</em></span>
    </label>
  )
}

function ValueRow({ keyRecord: key, names, save, busy }) {
  const savedBy = savedByApp(key, names)
  const warning = replaceWarning(key, names)
  const [shown, setShown] = useState(null)
  const [replacing, setReplacing] = useState(false)
  const [next, setNext] = useState('')
  const [multiline, setMultiline] = useState(Boolean(key.multiline))
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
          {shown === null
            ? <span aria-label="Hidden">••••••••••••</span>
            : <code className="settings-keys__value">{shown}</code>}
          {savedBy && <small>Saved by {savedBy}</small>}
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
          <ValueInput
            value={next}
            onChange={setNext}
            multiline={multiline}
            placeholder="Paste the new key"
            label="New key value"
            autoFocus
          />
          <MultilineToggle checked={multiline} onChange={setMultiline} />
          {warning && <small className="settings-keys__muted" role="note">{warning}</small>}
          <div className="settings-keys__actions">
            <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => { setReplacing(false); setNext('') }}>Cancel</button>
            <button type="submit" className="settings__btn settings__btn--sm" disabled={busy || !next}>Save</button>
          </div>
        </form>
      )}
    </>
  )
}

function SitesRows({ sites, onChange, busy, empty }) {
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
    if (await onChange([...sites, site])) {
      setAdding(false)
      setHost('')
      setPaths('/*')
    }
  }

  return (
    <>
      {sites.length === 0 && !adding && (
        <p className="settings-keys-card__row settings-keys__muted">{empty}</p>
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
            onClick={() => onChange(sites.filter((_, i) => i !== index))}
            disabled={busy}
          >
            <X width={16} height={16} aria-hidden="true" />
          </button>
        </div>
      ))}
      {adding ? (
        <form className="settings-keys-card__form" onSubmit={add} autoComplete="off">
          <input className="settings-keys__input" value={host} onChange={e => setHost(e.target.value)} placeholder="api.example.com" aria-label="Website" inputMode="url" autoFocus />
          <input className="settings-keys__input" value={paths} onChange={e => setPaths(e.target.value)} placeholder="/v1/*" aria-label="Paths" />
          <div className="settings-keys__actions">
            <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => setAdding(false)}>Cancel</button>
            <button type="submit" className="settings__btn settings__btn--sm" disabled={busy || !host.trim()}>Add website</button>
          </div>
        </form>
      ) : (
        <button type="button" className="settings-keys-card__row settings-keys-card__row--add" onClick={() => setAdding(true)}>
          <Plus width={16} height={16} aria-hidden="true" /> Add a website
        </button>
      )}
    </>
  )
}

/** Header or address parameter, as a controlled draft (see draftFromInject). */
function InjectFields({ draft, onChange }) {
  const set = change => onChange({ ...draft, ...change })
  return (
    <>
      <div className="settings-keys__segmented" role="radiogroup" aria-label="Sent as">
        {[['header', 'Header'], ['query', 'In the address']].map(([value, label]) => (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={draft.slot === value}
            className={`settings-keys__segment${draft.slot === value ? ' settings-keys__segment--on' : ''}`}
            onClick={() => set({ slot: value })}
          >{label}</button>
        ))}
      </div>
      {draft.slot === 'header' ? (
        <>
          <input className="settings-keys__input" value={draft.headerName} onChange={e => set({ headerName: e.target.value })} aria-label="Header name" />
          <input className="settings-keys__input" value={draft.prefix} onChange={e => set({ prefix: e.target.value })} placeholder="Nothing before the key" aria-label="Before the key" />
        </>
      ) : (
        <input className="settings-keys__input" value={draft.queryName} onChange={e => set({ queryName: e.target.value })} aria-label="Parameter name" />
      )}
    </>
  )
}

function SentAs({ keyRecord: key, save, busy }) {
  const inject = key.inject || BEARER
  const [draft, setDraft] = useState(() => draftFromInject(inject))

  function submit(event) {
    event.preventDefault()
    save(recordFor(key, { inject: injectFromDraft(draft) }))
  }

  return (
    <details className="settings-keys-sent">
      <summary>
        <span>Sent as</span>
        <code>{sentAs(inject)}</code>
      </summary>
      <form className="settings-keys-card__form" onSubmit={submit} autoComplete="off">
        <InjectFields draft={draft} onChange={setDraft} />
        <div className="settings-keys__actions">
          <button type="submit" className="settings__btn settings__btn--sm" disabled={busy}>Save</button>
        </div>
      </form>
    </details>
  )
}

function RecentActivity({ usage, names }) {
  const entries = usage
    .map(entry => ({ entry, text: usageSentence(entry, names) }))
    .filter(item => item.text)
    .slice(0, ACTIVITY_SHOWN)
  if (entries.length === 0) return <p className="settings-keys-card__row settings-keys__muted">Nothing yet.</p>
  return entries.map(({ entry, text }, index) => (
    <div className="settings-keys-card__row settings-keys-activity" key={`${entry.ts}-${index}`}>
      <span className="settings-keys-card__copy">
        <span className={entry.outcome === 'denied' || entry.outcome === 'failed' ? 'settings-keys__refused' : undefined}>{text}</span>
        {entry.host && entry.host !== '-' && <small>on {entry.host}{entry.path}</small>}
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

/**
 * Adding a key is a paste and an optional name. It is sent nowhere until an
 * app or the owner's agent first needs it and the owner approves the website;
 * Advanced covers the owner who already knows where it goes.
 */
function NewKey({ existingNames, onCreated }) {
  const queryClient = useQueryClient()
  const [typedName, setTypedName] = useState('')
  const [value, setValue] = useState('')
  const [multiline, setMultiline] = useState(false)
  const [agents, setAgents] = useState(true)
  const [host, setHost] = useState('')
  const [draft, setDraft] = useState(() => draftFromInject(BEARER))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const chosen = newKeyName(typedName, value, existingNames)
  const site = host.trim().toLowerCase()

  async function create(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const sites = site ? [{ host: site, methods: DEFAULT_METHODS, paths: ['/*'] }] : []
      await jsonOrThrow(await api.keys.put(chosen.name, {
        value,
        inject: site ? injectFromDraft(draft) : BEARER,
        grants: { apps: [], agents, sites },
        // Never overwrite a key that took this name meanwhile.
        create_only: true,
      }), 'Save key')
      await queryClient.invalidateQueries({ queryKey: KEYS_QUERY })
      onCreated(chosen.name)
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <form className="settings-keys-page" onSubmit={create} autoComplete="off">
      <section className="settings-list settings-keys-card">
        <div className="settings-keys-card__field">
          <span>Paste the key</span>
          <ValueInput value={value} onChange={setValue} multiline={multiline} placeholder="Paste any key, token or password" label="Paste the key" autoFocus />
          {chosen.service && !typedName.trim() && (
            <small className="settings-keys__hint">
              Looks like a {chosen.service} key, so it will be called <strong>{chosen.name}</strong>. Rename it any time.
            </small>
          )}
        </div>
        <label className="settings-keys-card__field">
          <span>Name (optional)</span>
          <input
            className="settings-keys__input"
            value={typedName}
            onChange={e => setTypedName(e.target.value)}
            placeholder={value ? chosen.name : 'e.g. my-github-token'}
            aria-invalid={typedName.trim() !== '' && (!chosen.valid || chosen.taken)}
          />
          {typedName.trim() !== '' && !chosen.valid && (
            <small className="settings-keys__error-inline">Use letters, numbers, - or _, starting with a letter.</small>
          )}
          {chosen.taken && <small className="settings-keys__error-inline">You already have a key called {chosen.name}.</small>}
        </label>
      </section>
      <p className="settings-keys__muted settings-keys-page__note">
        {site
          ? `Saved encrypted and sent only to ${site}.`
          : "Saved encrypted and sent nowhere yet. The first time an app or your agent needs it, you'll be asked where it may go."}
      </p>

      <details className="settings-keys-disclosure">
        <summary><span>Advanced</span></summary>
        <div className="settings-keys-disclosure__body">
          <MultilineToggle checked={multiline} onChange={setMultiline} />
          <div className="settings-keys-card__row settings-keys-card__row--plain">
            <span className="settings-keys-card__copy">
              <strong>Your agents may use it</strong>
              <small>They can read it, and send it to websites you approve.</small>
            </span>
            <Switch className="settings-keys-switch" checked={agents} onCheckedChange={setAgents} aria-label="Your agents may use it" />
          </div>
          <h3 className="settings-keys-disclosure__heading">I already know where it goes</h3>
          <input className="settings-keys__input" value={host} onChange={e => setHost(e.target.value)} placeholder="api.example.com" aria-label="Website" inputMode="url" />
          {site && <InjectFields draft={draft} onChange={setDraft} />}
        </div>
      </details>

      {error && <Alert color="danger" variant="soft" description={error} />}
      <div className="settings-keys__actions">
        <button type="submit" className="settings__btn" disabled={busy || !value || !chosen.valid || chosen.taken}>
          {busy ? 'Saving…' : 'Save key'}
        </button>
      </div>
    </form>
  )
}
