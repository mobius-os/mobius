/* Settings › Keys: the overview row, the Keys page (requests, your keys, the ones you said no to), and each key's own page. */
/* Every screen is built from the shared Settings blocks (SettingsPage.css):
   titled groups above grouped lists of rows, outline buttons in a row's
   trailing slot, and a chevron row that opens a panel beneath itself. */
import { useId, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import {
  ChevronDown, EditPencil, Eye, EyeOff, Globe, Key, Plus, Sparkles, Trash, X,
} from '@openai/apps-sdk-ui/components/Icon'
import { Switch } from '@openai/apps-sdk-ui/components/Switch'
import { api, isSharedBrowserAuth, jsonOrThrow } from '../../api/client.js'
import {
  BEARER, relativeTime, savedByApp, replaceWarning, siteLine, sentToLine, usedByLine, lastUseByCaller, usedByRows, newKeyName, requestFormKey, shownRequests, requestAppName, requestAppPaused, usageWhat,
  REQUESTS_SHOWN, VAULT_DOWN, keyParts, partLabel,
} from '../../lib/keysSettingsText.js'
import { keyFileDropProps } from '../../lib/keyFileDrop.js'
import KeyRequest from './KeyRequest.jsx'
import { KEYS_QUERY, useAppNames, useKeyUsage, useKeys } from './keysQueries.js'
import './KeysSection.css'
import { settingsKeyRequests } from '../../lib/keyRequestPlaces.js'

const DEFAULT_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
const ACTIVITY_SHOWN = 8
// A shared-browser guest never reads the vault (the routes refuse it).
const GUEST_LINE = 'Only the owner of this Möbius can see and manage keys.'
const ICON = { width: 18, height: 18, 'aria-hidden': true }
const BUTTON_ICON = { width: 15, height: 15, 'aria-hidden': true }

/**
 * One PUT carrying the key's current record with ``changes`` applied.
 * Settings mostly narrows a key: how it is sent, which apps may use it and
 * where the owner's agents may send it are set by the agent or app that asks,
 * and approved on that ask's card. Here the owner pauses, removes, replaces,
 * and adds a website for an app that already has the key, the one widening
 * left to Settings: an app that never asks (such as one whose saved keys moved
 * into the vault without sites) has no other way to get one.
 */
function recordFor(key, changes = {}) {
  return {
    inject: key.inject,
    grants: { ...key.grants, ...(changes.grants || {}) },
    // The vault refuses a save made from a stale copy of the key.
    revision: key.revision,
    ...(changes.value ? { value: changes.value } : {}),
    ...(changes.parts ? { parts: changes.parts } : {}),
  }
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

/**
 * The overview row's two lines, from the key list (null while it loads).
 * Key names stay on the Keys page: the overview row shows only a count, so
 * opening Settings never reveals which services the owner holds keys for.
 */
function summaryOf(data) {
  if (!data) return { title: 'Checking…', detail: 'Apps borrow a key without ever seeing it' }
  if (!data.available) return { title: 'The key vault isn’t running', detail: 'A restart usually brings it back' }
  const keys = data.keys || []
  if (keys.length === 0) return { title: 'No keys yet', detail: 'Apps borrow a key without ever seeing it' }
  const title = `${keys.length} ${keys.length === 1 ? 'key' : 'keys'}`
  const lastUsed = keys.map(key => key.last_used).filter(Boolean).sort().at(-1)
  return { title, detail: lastUsed ? `Last used ${relativeTime(lastUsed)}` : 'Not used yet' }
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

/** Where an agent's request was made: its chat, which may since be deleted. */
export function RequestChat({ request, onOpenChat }) {
  if (!request.chat_id) return null
  if (request.chat_title == null) {
    return <p className="settings-keys-request__chat">Asked in a chat that has since been deleted.</p>
  }
  return (
    <p className="settings-keys-request__chat">
      Asked in “{request.chat_title}”.{' '}
      {onOpenChat && (
        <button type="button" className="settings-keys-request__open" onClick={() => onOpenChat(request.chat_id)}>
          Open the chat
        </button>
      )}
    </p>
  )
}

/** The Keys page: requests waiting here, your keys, and the ones you said no to. */
export function KeysPage({ onOpenKey, onAddKey, onOpenChat }) {
  const keysQuery = useKeys()
  const { names } = useAppNames()
  const queryClient = useQueryClient()
  const [showAll, setShowAll] = useState(false)
  if (isSharedBrowserAuth()) return <div className="settings-keys-page"><p className="settings-group__desc">{GUEST_LINE}</p></div>
  const data = keysQuery.data
  const keys = data?.keys || []
  // Every request your agents made waits here too, whatever became of its card.
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
        <Alert color="warning" variant="soft" description={VAULT_DOWN} />
      )}

      {pending.length > 0 && (
        <Group title="Waiting for you" desc="Your agents asked for these. Answer here, or on the card in the chat that asked.">
          <div className="settings-keys-requests">
            {shownRequests(pending, showAll).map(request => (
              <div key={requestFormKey(request)} className="settings-keys-request">
                <KeyRequest
                  request={{ ...request, app_paused: requestAppPaused(keys, request) }}
                  names={names}
                  onAnswered={refresh}
                />
                <RequestChat request={request} onOpenChat={onOpenChat} />
              </div>
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
  const who = requestAppName(request, names)
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
  const { names } = useAppNames()
  const { save, error, busy } = useSaveKey(key.name)
  const usageQuery = useKeyUsage(key.name)
  const usage = usageQuery.data?.usage || []
  const grantedApps = key.grants?.apps || []
  const rows = usedByRows(key, names, lastUseByCaller(usage))
  const setGrants = grants => save(recordFor(key, { grants }))
  const updateApp = (id, change) => setGrants({
    apps: grantedApps.map(grant => (grant.id === id ? { ...grant, ...change } : grant)),
  })

  return (
    <div className="settings-keys-page">
      {error && <Alert color="danger" variant="soft" description={error} />}

      <Group title="Key">
        <div className="settings-list">
          {keyParts(key).map(part => (
            <ValueRow key={part.name} keyRecord={key} part={part} names={names} save={save} busy={busy} />
          ))}
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
        </div>
      </Group>

      <Group title="Websites" desc="Where you and your agents may send it. Each app has its own list under Used by.">
        <div className="settings-list">
          <SitesRows
            sites={key.grants?.sites || []}
            onChange={sites => setGrants({ sites })}
            busy={busy}
            empty="None yet. Your agent asks you the first time it needs to send it somewhere."
          />
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
              canAdd
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

/**
 * The value field: one line (masked unless ``plain``), or a box for keys with
 * several lines that also takes a dropped key file.
 */
function ValueInput({ id, value, onChange, multiline, placeholder, label, autoFocus, plain = false }) {
  return multiline ? (
    <textarea
      id={id}
      className="settings-input settings-input--multiline"
      value={value}
      onChange={e => onChange(e.target.value)}
      {...keyFileDropProps(onChange)}
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
      type={plain ? 'text' : 'password'}
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

/**
 * One part of a key: the only one (``value``) for most keys, or each named
 * part of a key sent in several pieces. A plain part (an id, a username)
 * shows its text; a secret one stays hidden until Show. Replace changes only
 * this part.
 */
function ValueRow({ keyRecord: key, part, names, save, busy }) {
  const savedBy = savedByApp(key, names)
  const warning = replaceWarning(key, names)
  const single = keyParts(key).length === 1 && part.name === 'value'
  const label = partLabel(part)
  const [revealed, setShown] = useState(null)
  // A plain part always shows its current text; a secret one only once revealed.
  const shown = part.secret ? revealed : part.text
  const [replacing, setReplacing] = useState(false)
  const [next, setNext] = useState('')
  const [multiline, setMultiline] = useState(Boolean(part.multiline))
  const [error, setError] = useState('')

  async function show() {
    setError('')
    try {
      setShown((await jsonOrThrow(await api.keys.reveal(key.name, part.name), 'Show key')).value)
    } catch (err) {
      setError(err.message)
    }
  }

  async function replace(event) {
    event.preventDefault()
    const change = single ? { value: next } : { parts: [{ name: part.name, value: next }] }
    if (await save(recordFor(key, change))) {
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
          {!single && <span className="settings-item__sub">{label}</span>}
          {shown === null
            ? <span className="settings-item__title settings-keys-value__mask" aria-label="Hidden">••••••••••••</span>
            : <code className="settings-keys__value">{shown}</code>}
          <span className="settings-item__sub">
            {!part.secret ? 'Not secret: shown as typed' : savedBy ? `Saved by ${savedBy}` : 'Encrypted on this Möbius'}
          </span>
        </span>
        <span className="settings-actions settings-keys-value__actions">
          {part.secret && (
            <button
              type="button"
              className="settings__btn settings__btn--outline settings__btn--sm"
              aria-label={single ? undefined : `${shown === null ? 'Show' : 'Hide'} ${label}`}
              onClick={() => (shown === null ? show() : setShown(null))}
            >
              {shown === null ? <Eye {...BUTTON_ICON} /> : <EyeOff {...BUTTON_ICON} />}
              {shown === null ? 'Show' : 'Hide'}
            </button>
          )}
          {!replacing && (
            <button
              type="button"
              className="settings__btn settings__btn--outline settings__btn--sm"
              aria-label={single ? undefined : `Replace ${label}`}
              onClick={() => setReplacing(true)}
            >
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
            plain={!part.secret}
            placeholder={single ? 'Paste the new key' : `Paste the new ${label}`}
            label={single ? 'New key value' : `New ${label}`}
            autoFocus
          />
          {part.secret && <SwitchRow title="It has several lines" sub="A certificate or key file" checked={multiline} onChange={setMultiline} />}
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

/**
 * Website rows inside a list; each can be removed. ``canAdd`` adds an Add row
 * that opens a small form in place (only an app's own list has one).
 */
export function SitesRows({ sites, onChange, busy, empty, canAdd = false }) {
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
      {canAdd && (adding ? (
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
      ))}
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
 * app or the owner's agent first needs it: that ask sets where it goes and how,
 * and the owner approves it on the ask's card.
 */
function NewKey({ existingNames, onCreated }) {
  const queryClient = useQueryClient()
  const [typedName, setTypedName] = useState('')
  const [value, setValue] = useState('')
  const [multiline, setMultiline] = useState(false)
  const [agents, setAgents] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const valueId = useId()
  const nameId = useId()
  const chosen = newKeyName(typedName, value, existingNames)
  const nameProblem = typedName.trim() !== '' && !chosen.valid
    ? 'Use letters, numbers, - or _, starting with a letter.'
    : chosen.taken ? `You already have a key called ${chosen.name}.` : ''

  async function create(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      await jsonOrThrow(await api.keys.put(chosen.name, {
        value,
        // The first ask for it sets where it really goes.
        inject: BEARER,
        grants: { apps: [], agents, sites: [] },
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

        <p className="settings-field__hint">
          Saved encrypted and sent nowhere yet. The first time an app or your agent needs it, you'll be asked where it may go.
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
