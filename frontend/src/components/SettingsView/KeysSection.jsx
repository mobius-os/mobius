/* Settings › Keys: the overview row, the Keys page (requests, your keys, the ones you said no to), and each key's own page. */
/* Every screen is built from the shared Settings blocks (SettingsPage.css):
   titled groups above grouped lists of rows, outline buttons in a row's
   trailing slot, and a chevron row that opens a panel beneath itself. */
import { useId, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import {
  ChevronDown, Code, EditPencil, Eye, EyeOff, Globe, Key, Plus, Sparkles, Trash, X,
} from '@openai/apps-sdk-ui/components/Icon'
import { Switch } from '@openai/apps-sdk-ui/components/Switch'
import { api, isSharedBrowserAuth, jsonOrThrow } from '../../api/client.js'
import {
  relativeTime, savedByApp, replaceWarning, siteLine, sentAs, sentToLine, usedByLine, lastUseByCaller, usedByRows, newKeyName, requestFormKey, shownRequests, requestAppName, requestAppPaused, usageWhat,
  REQUESTS_SHOWN,
} from '../../lib/keysSettingsText.js'
import KeyRequest, { BEARER } from './KeyRequest.jsx'
import { KEYS_QUERY, useAppNames, useKeyUsage, useKeys } from './keysQueries.js'
import './KeysSection.css'
import { settingsKeyRequests } from '../../lib/keyRequestPlaces.js'

const DEFAULT_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
const ACTIVITY_SHOWN = 8
// A shared-browser guest never reads the vault (the routes refuse it).
const GUEST_LINE = 'Only the owner of this Möbius can see and manage keys.'
const ICON = { width: 18, height: 18, 'aria-hidden': true }
const BUTTON_ICON = { width: 15, height: 15, 'aria-hidden': true }

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

/** A titled Settings section: quiet title and description above its own list. */
function Group({ title, desc, aside, label, children }) {
  const id = useId()
  return (
    <section className="settings-group" aria-labelledby={title ? id : undefined} aria-label={title ? undefined : label}>
      {title && (
        <div className="settings-group__head">
          <div className="settings-group__heading">
            <h2 className="settings-group__title" id={id}>{title}</h2>
            {desc && <p className="settings-group__desc">{desc}</p>}
          </div>
          {aside}
        </div>
      )}
      {children}
    </section>
  )
}

/** A row whose only content is a quiet line (empty states). */
function QuietRow({ children }) {
  return (
    <div className="settings-item settings-keys-quiet">
      <span className="settings-item__sub settings-item__sub--wrap">{children}</span>
    </div>
  )
}

// -- overview row and the Keys page -----------------------------------------

const SUMMARY_NAMES = 3

/** The overview row's two lines, from the key list (null while it loads). */
function summaryOf(data) {
  if (!data) return { title: 'Checking…', detail: 'Apps borrow a key without ever seeing it' }
  if (!data.available) return { title: "The key vault isn't running", detail: 'A restart usually brings it back' }
  const keys = data.keys || []
  if (keys.length === 0) return { title: 'No keys yet', detail: 'Apps borrow a key without ever seeing it' }
  const title = keys.length <= SUMMARY_NAMES ? keys.map(key => key.name).join(', ') : `${keys.length} keys`
  const lastUsed = keys.map(key => key.last_used).filter(Boolean).sort().at(-1)
  const count = `${keys.length} ${keys.length === 1 ? 'key' : 'keys'}`
  return { title, detail: lastUsed ? `${count} · last used ${relativeTime(lastUsed)}` : `${count} · not used yet` }
}

/** Settings › Keys as one row, like Notifications elsewhere; the row opens the Keys page. */
export default function KeysSummary({ onOpen }) {
  const keysQuery = useKeys()
  const guest = isSharedBrowserAuth()
  const data = guest ? null : keysQuery.data
  const waiting = settingsKeyRequests(data).length
  const { title, detail } = keysQuery.error && !guest
    ? { title: "Keys couldn't load", detail: keysQuery.error.message }
    : summaryOf(data)
  return (
    <section className="settings-group" aria-labelledby="settings-keys-title" id="settings-keys">
      <div className="settings-group__head">
        <div className="settings-group__heading">
          <h2 className="settings-group__title" id="settings-keys-title">Keys</h2>
          <p className="settings-group__desc">Apps borrow a key without ever seeing it. You and your agents can read them.</p>
        </div>
      </div>
      <div className="settings-list">
        {guest ? (
          <div className="settings-item">
            <span className="settings-item__icon" aria-hidden="true"><Key {...ICON} /></span>
            <span className="settings-item__body">
              <span className="settings-item__sub settings-item__sub--wrap">{GUEST_LINE}</span>
            </span>
          </div>
        ) : (
          // Opens its own page, so no arrow: arrows in Settings mean "expands here".
          <button type="button" className="settings-item" onClick={onOpen} aria-label={`Keys: ${title}. ${detail}`}>
            <span className="settings-item__icon" aria-hidden="true"><Key {...ICON} /></span>
            <span className="settings-item__body">
              <span className="settings-item__title">{title}</span>
              <span className="settings-item__sub">{detail}</span>
            </span>
            {waiting > 0 && <span className="settings-badge settings-badge--accent">{waiting} need{waiting === 1 ? 's' : ''} you</span>}
          </button>
        )}
      </div>
    </section>
  )
}

/** The Keys page: requests waiting here, your keys, and the ones you said no to. */
export function KeysPage({ onOpenKey, onAddKey }) {
  const keysQuery = useKeys()
  const { names } = useAppNames()
  const queryClient = useQueryClient()
  const [showAll, setShowAll] = useState(false)
  if (isSharedBrowserAuth()) return <div className="settings-keys-page"><p className="settings-group__desc">{GUEST_LINE}</p></div>
  const data = keysQuery.data
  const keys = data?.keys || []
  // Asks are answered where they were made; only an agent's with no chat lands here.
  const pending = settingsKeyRequests(data)
  const denied = (data?.requests || []).filter(request => request.status === 'denied')
  const refresh = () => queryClient.invalidateQueries({ queryKey: KEYS_QUERY })

  return (
    <div className="settings-keys-page">
      <p className="settings-group__desc settings-keys-page__lede">
        Apps borrow a key without ever seeing it, and only on websites you allow. You and your agents can read them.
      </p>
      {keysQuery.error && <Alert color="danger" variant="soft" description={keysQuery.error.message} />}
      {data && !data.available && (
        <Alert color="warning" variant="soft" description="The key vault isn't running. It starts with Möbius; a restart usually brings it back." />
      )}

      {pending.length > 0 && (
        <Group title="Waiting for you" desc="Your agent asked for these outside a chat.">
          <div className="settings-keys-requests">
            {shownRequests(pending, showAll).map(request => (
              <KeyRequest
                key={requestFormKey(request)}
                request={{ ...request, app_paused: requestAppPaused(keys, request) }}
                names={names}
                onAnswered={refresh}
              />
            ))}
          </div>
          {!showAll && pending.length > REQUESTS_SHOWN && (
            <button type="button" className="settings__btn settings__btn--outline settings__btn--sm settings-keys__more" onClick={() => setShowAll(true)}>
              Show {pending.length - REQUESTS_SHOWN} more {pending.length - REQUESTS_SHOWN === 1 ? 'request' : 'requests'}
            </button>
          )}
        </Group>
      )}

      {data?.available && (
        <Group
          title="Your keys"
          aside={(
            <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={onAddKey}>
              <Plus {...BUTTON_ICON} /> Add
            </button>
          )}
        >
          <div className="settings-list">
            {keys.length === 0 && (
              <div className="settings-item">
                <span className="settings-item__icon" aria-hidden="true"><Key {...ICON} /></span>
                <span className="settings-item__body">
                  <span className="settings-item__title">No keys yet</span>
                  <span className="settings-item__sub settings-item__sub--wrap">Add one, or approve the first time an app or your agent asks.</span>
                </span>
              </div>
            )}
            {keys.map(key => (
              // Opens its own page, so no arrow: arrows in Settings mean "expands here".
              <button key={key.name} type="button" className="settings-item" onClick={() => onOpenKey(key.name)}>
                <span className="settings-item__icon" aria-hidden="true"><Key {...ICON} /></span>
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
          </div>
        </Group>
      )}

      {data?.available && denied.length > 0 && (
        <Group title="You said no" desc="These can't ask for the key again until you let them.">
          <div className="settings-list">
            {denied.map(request => (
              <DeniedRequest key={request.id} request={request} names={names} onAnswered={refresh} />
            ))}
          </div>
        </Group>
      )}
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
  const who = request.requester === 'agent' ? 'Your agent' : requestAppName(request, names)
  return (
    <div className="settings-item">
      <span className="settings-item__body">
        <span className="settings-item__title">{who}</span>
        <span className="settings-item__sub settings-item__sub--wrap">Asked to use {request.key}</span>
      </span>
      <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={forget} disabled={busy}>
        Let it ask again
      </button>
    </div>
  )
}

// -- one key's page ----------------------------------------------------------

/** The line under a key page's title: where the key goes. */
export function KeyPageSubtitle({ name }) {
  const keysQuery = useKeys()
  if (isSharedBrowserAuth()) return null
  if (name === '') return <span>Saved encrypted on this Möbius</span>
  const key = (keysQuery.data?.keys || []).find(item => item.name === name)
  return key ? <span>{sentToLine(key)}</span> : null
}

/** A key's own page; ``name`` '' is the new-key form. */
export function KeyPage({ name, onOpenKey, onClose }) {
  const keysQuery = useKeys()
  if (isSharedBrowserAuth()) return <div className="settings-keys-page"><p className="settings-group__desc">{GUEST_LINE}</p></div>
  if (name === '') return <NewKey existingNames={(keysQuery.data?.keys || []).map(key => key.name)} onCreated={onOpenKey} />
  const key = (keysQuery.data?.keys || []).find(item => item.name === name)
  if (keysQuery.isPending) return null
  if (!key) {
    return (
      <div className="settings-keys-page">
        <p className="settings-group__desc">This key no longer exists.</p>
        <div className="settings-actions settings-keys-page__actions">
          <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={onClose}>Back to Keys</button>
        </div>
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
  const usageQuery = useKeyUsage(key.name)
  const usage = usageQuery.data?.usage || []
  const grantedApps = key.grants?.apps || []
  const grantedIds = grantedApps.map(grant => grant.id)
  const ungranted = apps.filter(app => !grantedIds.includes(app.id))
  const rows = usedByRows(key, names, lastUseByCaller(usage))
  const setGrants = grants => save(recordFor(key, { grants }))
  const updateApp = (id, change) => setGrants({
    apps: grantedApps.map(grant => (grant.id === id ? { ...grant, ...change } : grant)),
  })
  // A new app starts with no sites; its own row under Used by adds the ones it may reach.
  const grantApp = app => save(recordFor(key, {
    grants: { apps: [...grantedApps, { id: app.id, sites: [] }] },
    installations: { [app.id]: app.installation },
  }))

  return (
    <div className="settings-keys-page">
      {error && <Alert color="danger" variant="soft" description={error} />}

      <Group title="Key">
        <div className="settings-list">
          <ValueRow keyRecord={key} names={names} save={save} busy={busy} />
        </div>
      </Group>

      <Group title="Used by" desc="Switch one off to pause it. Nothing is deleted.">
        <div className="settings-list">
          {grantedApps.map(grant => (
            <AppRow
              key={grant.id}
              grant={grant}
              row={rows.apps.find(row => row.id === grant.id)}
              busy={busy}
              onSwitch={on => updateApp(grant.id, { paused: !on })}
              onSites={sites => updateApp(grant.id, { sites })}
              onRemove={() => setGrants({ apps: grantedApps.filter(other => other.id !== grant.id) })}
            />
          ))}
          <div className="settings-item">
            <span className="settings-item__icon" aria-hidden="true"><Sparkles {...ICON} /></span>
            <span className="settings-item__body">
              <span className="settings-item__title">{rows.agents.label}</span>
              <span className="settings-item__sub settings-item__sub--wrap">{rows.agents.sub}</span>
            </span>
            <Switch
              className="settings-switch"
              checked={rows.agents.on}
              onCheckedChange={on => setGrants({ agents: on })}
              disabled={busy}
              aria-label="Your agents can use it"
            />
          </div>
          {ungranted.length > 0 && (
            <label className="settings-item settings-keys-add">
              <span className="settings-item__icon settings-keys-add__icon" aria-hidden="true"><Plus {...ICON} /></span>
              <select
                className="settings-keys-add__select"
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
        </div>
      </Group>

      <Group title="Websites" desc="Where you and your agents may send it. Each app has its own list under Used by.">
        <div className="settings-list">
          <SitesRows
            sites={key.grants?.sites || []}
            onChange={sites => setGrants({ sites })}
            busy={busy}
            empty="None yet. You and your agents can't send it until you add one or approve your agent's request."
          />
        </div>
      </Group>

      <Group
        title="How it's sent"
        desc={key.multiline ? 'This key has several lines, so it can be sent only in the address, never as a header.' : undefined}
      >
        <div className="settings-list">
          <SentAs keyRecord={key} save={save} busy={busy} />
        </div>
      </Group>

      <Group title="Recent activity">
        <div className="settings-list">
          <RecentActivity usage={usage} names={names} />
        </div>
      </Group>

      <DeleteKey name={key.name} onDeleted={onDeleted} />
    </div>
  )
}

/** An app allowed to use the key: tap to see its own websites; the switch pauses it. */
function AppRow({ grant, row, busy, onSwitch, onSites, onRemove }) {
  const [open, setOpen] = useState(false)
  const label = row?.label || `App ${grant.id}`
  const count = grant.sites.length
  const sites = count === 0 ? 'No websites yet' : count === 1 ? siteLine(grant.sites[0]) : `${count} websites`
  const used = row?.sub?.startsWith('Last used') ? row.sub.toLowerCase() : ''
  const sub = grant.paused ? 'Switched off' : [sites, used].filter(Boolean).join(' · ')
  return (
    <div>
      <div className="settings-item">
        <button type="button" className="settings-keys-expand" aria-expanded={open} onClick={() => setOpen(!open)}>
          <span className="settings-item__icon settings-keys-initial" aria-hidden="true">{row?.initial || '?'}</span>
          <span className="settings-item__body">
            <span className="settings-item__title">{label}</span>
            <span className={`settings-item__sub${count === 0 && !grant.paused ? ' settings-keys__warn' : ''}`}>{sub}</span>
          </span>
          <ChevronDown width={16} height={16} className="settings-item__chevron" aria-hidden="true" />
        </button>
        <Switch
          className="settings-switch"
          checked={!grant.paused}
          onCheckedChange={onSwitch}
          disabled={busy}
          aria-label={`${label} can use it`}
        />
      </div>
      {open && (
        <div className="settings-item-panel settings-keys-panel">
          <div className="settings-list">
            <SitesRows
              sites={grant.sites}
              onChange={onSites}
              busy={busy}
              empty={`No websites yet. ${label} can't send this key until you add one.`}
            />
          </div>
          <div className="settings-actions">
            <button type="button" className="settings__btn settings__btn--danger settings__btn--sm" onClick={onRemove} disabled={busy}>
              Stop {label} using it
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

/** The value field: one masked line, or a box for keys with several lines. */
function ValueInput({ id, value, onChange, multiline, placeholder, label, autoFocus }) {
  return multiline ? (
    <textarea
      id={id}
      className="settings-input settings-input--multiline"
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
      id={id}
      className="settings-input"
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

/** A label and description on the left, a switch on the right. */
function SwitchRow({ title, sub, checked, onChange, disabled }) {
  return (
    <div className="settings-switch-row">
      <span>
        <span className="settings-switch-row__title">{title}</span>
        {sub && <span className="settings-switch-row__sub">{sub}</span>}
      </span>
      <Switch className="settings-switch" checked={checked} onCheckedChange={onChange} disabled={disabled} aria-label={title} />
    </div>
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
      <div className="settings-item settings-keys-value">
        <span className="settings-item__icon" aria-hidden="true"><Key {...ICON} /></span>
        <span className="settings-item__body">
          {shown === null
            ? <span className="settings-item__title settings-keys-value__mask" aria-label="Hidden">••••••••••••</span>
            : <code className="settings-keys__value">{shown}</code>}
          <span className="settings-item__sub">{savedBy ? `Saved by ${savedBy}` : 'Encrypted on this Möbius'}</span>
        </span>
        <span className="settings-actions settings-keys-value__actions">
          <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => (shown === null ? show() : setShown(null))}>
            {shown === null ? <Eye {...BUTTON_ICON} /> : <EyeOff {...BUTTON_ICON} />}
            {shown === null ? 'Show' : 'Hide'}
          </button>
          {!replacing && (
            <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => setReplacing(true)}>
              <EditPencil {...BUTTON_ICON} /> Replace
            </button>
          )}
        </span>
      </div>
      {error && <p className="settings-item-panel settings-message settings-message--error" role="alert">{error}</p>}
      {replacing && (
        <form className="settings-item-panel settings-keys-form" onSubmit={replace} autoComplete="off">
          <ValueInput
            value={next}
            onChange={setNext}
            multiline={multiline}
            placeholder="Paste the new key"
            label="New key value"
            autoFocus
          />
          <SwitchRow title="It has several lines" sub="A certificate or key file" checked={multiline} onChange={setMultiline} />
          {warning && <p className="settings-field__hint" role="note">{warning}</p>}
          <div className="settings-actions">
            <button type="submit" className="settings__btn settings__btn--sm" disabled={busy || !next}>Save</button>
            <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => { setReplacing(false); setNext('') }}>Cancel</button>
          </div>
        </form>
      )}
    </>
  )
}

/** Website rows inside a list, with an Add row that opens a small form in place. */
function SitesRows({ sites, onChange, busy, empty }) {
  const [adding, setAdding] = useState(false)
  const [host, setHost] = useState('')
  const [paths, setPaths] = useState('/*')
  const hostId = useId()
  const pathsId = useId()

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
      {sites.length === 0 && !adding && <QuietRow>{empty}</QuietRow>}
      {sites.map((site, index) => (
        <div className="settings-item" key={`${site.host}-${index}`}>
          <span className="settings-item__icon" aria-hidden="true"><Globe {...ICON} /></span>
          <span className="settings-item__body">
            <span className="settings-item__title settings-item__title--wrap">{site.host}</span>
            <span className="settings-item__sub settings-item__sub--wrap">{site.paths.join(', ')} · {site.methods.join(', ')}</span>
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
        <form className="settings-keys-form settings-keys-form--row" onSubmit={add} autoComplete="off">
          <label className="settings-field" htmlFor={hostId}>
            <span className="settings-field__label">Website</span>
            <input id={hostId} className="settings-input" value={host} onChange={e => setHost(e.target.value)} placeholder="api.example.com" inputMode="url" autoFocus />
          </label>
          <label className="settings-field" htmlFor={pathsId}>
            <span className="settings-field__label">Paths</span>
            <input id={pathsId} className="settings-input" value={paths} onChange={e => setPaths(e.target.value)} placeholder="/v1/*" />
            <span className="settings-field__hint">Separate several with commas. /* means every page.</span>
          </label>
          <div className="settings-actions">
            <button type="submit" className="settings__btn settings__btn--sm" disabled={busy || !host.trim()}>Add website</button>
            <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => setAdding(false)}>Cancel</button>
          </div>
        </form>
      ) : (
        <button type="button" className="settings-item settings-keys-add" onClick={() => setAdding(true)}>
          <span className="settings-item__icon settings-keys-add__icon" aria-hidden="true"><Plus {...ICON} /></span>
          <span className="settings-item__title">Add a website</span>
        </button>
      )}
    </>
  )
}

/** Header or address parameter, as a controlled draft (see draftFromInject). */
function InjectFields({ draft, onChange }) {
  const set = change => onChange({ ...draft, ...change })
  const nameId = useId()
  const prefixId = useId()
  return (
    <>
      <div className="settings-seg" role="radiogroup" aria-label="Sent as">
        {[['header', 'Header'], ['query', 'In the address']].map(([value, label]) => (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={draft.slot === value}
            className={`settings-seg__option${draft.slot === value ? ' settings-seg__option--on' : ''}`}
            onClick={() => set({ slot: value })}
          >{label}</button>
        ))}
      </div>
      {draft.slot === 'header' ? (
        <div className="settings-keys-form__pair">
          <label className="settings-field" htmlFor={nameId}>
            <span className="settings-field__label">Header name</span>
            <input id={nameId} className="settings-input" value={draft.headerName} onChange={e => set({ headerName: e.target.value })} />
          </label>
          <label className="settings-field" htmlFor={prefixId}>
            <span className="settings-field__label">Before the key</span>
            <input id={prefixId} className="settings-input" value={draft.prefix} onChange={e => set({ prefix: e.target.value })} placeholder="Nothing" />
          </label>
        </div>
      ) : (
        <label className="settings-field" htmlFor={nameId}>
          <span className="settings-field__label">Parameter name</span>
          <input id={nameId} className="settings-input" value={draft.queryName} onChange={e => set({ queryName: e.target.value })} />
        </label>
      )}
    </>
  )
}

function SentAs({ keyRecord: key, save, busy }) {
  const inject = key.inject || BEARER
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(() => draftFromInject(inject))

  async function submit(event) {
    event.preventDefault()
    if (await save(recordFor(key, { inject: injectFromDraft(draft) }))) setOpen(false)
  }

  return (
    <>
      <button type="button" className="settings-item" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="settings-item__icon" aria-hidden="true"><Code {...ICON} /></span>
        <span className="settings-item__body">
          <span className="settings-item__title">{inject.type === 'query' ? 'In the address' : 'As a header'}</span>
          <code className="settings-item__sub settings-keys-code">{sentAs(inject)}</code>
        </span>
        <ChevronDown width={16} height={16} className="settings-item__chevron" aria-hidden="true" />
      </button>
      {open && (
        <form className="settings-item-panel settings-keys-form" onSubmit={submit} autoComplete="off">
          <InjectFields draft={draft} onChange={setDraft} />
          <div className="settings-actions">
            <button type="submit" className="settings__btn settings__btn--sm" disabled={busy}>Save</button>
            <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => { setOpen(false); setDraft(draftFromInject(inject)) }}>Cancel</button>
          </div>
        </form>
      )}
    </>
  )
}

function RecentActivity({ usage, names }) {
  const entries = usage
    .map(entry => ({ entry, text: usageWhat(entry, names) }))
    .filter(item => item.text)
    .slice(0, ACTIVITY_SHOWN)
  if (entries.length === 0) return <QuietRow>Nothing yet.</QuietRow>
  return entries.map(({ entry, text }, index) => {
    const refused = entry.outcome === 'denied' || entry.outcome === 'failed'
    return (
      <div className="settings-item settings-keys-activity" key={`${entry.ts}-${index}`}>
        <span className="settings-item__body">
          <span className={`settings-item__title settings-item__title--wrap${refused ? ' settings-keys__refused' : ''}`}>{text}</span>
          {entry.host && entry.host !== '-' && <span className="settings-item__sub settings-item__sub--wrap">{entry.host}{entry.path}</span>}
        </span>
        {entry.ts && <span className="settings-item__trail">{relativeTime(entry.ts)}</span>}
      </div>
    )
  })
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
    <Group label="Delete key">
      <div className="settings-list">
        <div className="settings-item">
          <span className="settings-item__body">
            <span className="settings-item__title">Delete this key</span>
            <span className="settings-item__sub settings-item__sub--wrap">Apps and agents lose it at once. This can't be undone.</span>
          </span>
          {!confirm && (
            <button type="button" className="settings__btn settings__btn--danger settings__btn--sm" onClick={() => setConfirm(true)}>
              <Trash {...BUTTON_ICON} /> Delete
            </button>
          )}
        </div>
        {confirm && (
          <div className="settings-item-panel">
            <p className="settings-item-panel__text">Delete {name} for good?</p>
            <div className="settings-actions">
              <button type="button" className="settings__btn settings__btn--sm settings-keys__danger" onClick={remove} disabled={busy}>
                {busy ? 'Deleting…' : 'Delete for good'}
              </button>
              <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={() => setConfirm(false)} disabled={busy}>Keep it</button>
            </div>
          </div>
        )}
      </div>
      {error && <p className="settings-message settings-message--error" role="alert">{error}</p>}
    </Group>
  )
}

/**
 * Adding a key is a paste and an optional name. It is sent nowhere until an
 * app or the owner's agent first needs it and the owner approves the website;
 * "I already know which website it's for" covers the owner who already knows.
 */
function NewKey({ existingNames, onCreated }) {
  const queryClient = useQueryClient()
  const [typedName, setTypedName] = useState('')
  const [value, setValue] = useState('')
  const [multiline, setMultiline] = useState(false)
  const [agents, setAgents] = useState(true)
  const [choosingSite, setChoosingSite] = useState(false)
  const [host, setHost] = useState('')
  const [draft, setDraft] = useState(() => draftFromInject(BEARER))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const valueId = useId()
  const nameId = useId()
  const hostId = useId()
  const chosen = newKeyName(typedName, value, existingNames)
  const site = choosingSite ? host.trim().toLowerCase() : ''
  const nameProblem = typedName.trim() !== '' && !chosen.valid
    ? 'Use letters, numbers, - or _, starting with a letter.'
    : chosen.taken ? `You already have a key called ${chosen.name}.` : ''

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
      <div className="settings-card settings-keys-form settings-keys-form--card">
        <label className="settings-field" htmlFor={valueId}>
          <span className="settings-field__label">Key</span>
          <ValueInput id={valueId} value={value} onChange={setValue} multiline={multiline} placeholder="Paste any key, token or password" label="Paste the key" autoFocus />
          {chosen.service && !typedName.trim() && (
            <span className="settings-field__hint">
              Looks like a {chosen.service} key, so it will be called <strong>{chosen.name}</strong>.
            </span>
          )}
        </label>

        <label className="settings-field" htmlFor={nameId}>
          <span className="settings-field__label">Name <span className="settings-field__optional">optional</span></span>
          <input
            id={nameId}
            className="settings-input"
            value={typedName}
            onChange={e => setTypedName(e.target.value)}
            placeholder={value ? chosen.name : 'e.g. my-github-token'}
            aria-invalid={Boolean(nameProblem)}
          />
          {nameProblem && <span className="settings-field__hint settings-field__hint--error">{nameProblem}</span>}
        </label>

        <div className="settings-keys-form__switches">
          <SwitchRow title="Your agents may use it" sub="They can read it, and send it to websites you approve." checked={agents} onChange={setAgents} />
          <SwitchRow title="It has several lines" sub="A certificate or key file" checked={multiline} onChange={setMultiline} />
        </div>

        {choosingSite ? (
          <div className="settings-keys-form__where">
            <label className="settings-field" htmlFor={hostId}>
              <span className="settings-field__label">Website</span>
              <input id={hostId} className="settings-input" value={host} onChange={e => setHost(e.target.value)} placeholder="api.example.com" inputMode="url" autoFocus />
            </label>
            {site && <InjectFields draft={draft} onChange={setDraft} />}
          </div>
        ) : (
          <button type="button" className="settings-link settings-keys-form__link" onClick={() => setChoosingSite(true)}>
            I already know which website it's for
          </button>
        )}

        <p className="settings-field__hint">
          {site
            ? `Saved encrypted and sent only to ${site}.`
            : "Saved encrypted and sent nowhere yet. The first time an app or your agent needs it, you'll be asked where it may go."}
        </p>
        {error && <Alert color="danger" variant="soft" description={error} />}
        <div className="settings-actions">
          <button type="submit" className="settings__btn" disabled={busy || !value || !chosen.valid || chosen.taken}>
            {busy ? 'Saving…' : 'Save key'}
          </button>
        </div>
      </div>
    </form>
  )
}
