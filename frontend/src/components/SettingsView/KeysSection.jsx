/* Settings › Keys: the overview list, app and agent requests, and each key's own page. */
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Key, Plus, X } from '@openai/apps-sdk-ui/components/Icon'
import { Switch } from '@openai/apps-sdk-ui/components/Switch'
import { api, jsonOrThrow } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import './KeysSection.css'

const KEYS_QUERY = ['settings', 'keys']
const NAME_PATTERN = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/
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

// -- decisions (pure, unit-tested) -------------------------------------------

/**
 * Two installed apps may share a name; a grant or request must still say
 * which one it is, so a shared name carries the app's number.
 */
export function distinctAppNames(apps) {
  const counts = {}
  for (const app of apps) counts[app.name] = (counts[app.name] || 0) + 1
  return apps.map(app => (counts[app.name] > 1 ? { ...app, name: `${app.name} (#${app.id})` } : app))
}

function appLabel(names, id) {
  return names[id] || `App ${id}`
}

export function relativeTime(iso, now = Date.now()) {
  if (!iso) return ''
  const seconds = Math.max(0, (now - Date.parse(iso)) / 1000)
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`
  return `${Math.round(seconds / 86400)} d ago`
}

/**
 * The app that saved this key as its own secret, or null for the owner's key.
 * A new value from anyone else makes the key the owner's and withdraws that
 * app's access, so Settings names the app before a Replace.
 */
export function savedByApp(key, names) {
  return key.origin ? appLabel(names, key.origin.app) : null
}

export function replaceWarning(key, names) {
  const app = savedByApp(key, names)
  return app
    ? `${app} saved this key. A new value makes it yours, and ${app} stops using it until you let it again under Advanced › Let an app use it.`
    : null
}

/** A site as the owner reads it; `*` matches across `/`, so `/*` is every path. */
export function siteLine(site) {
  const paths = site.paths.includes('/*') ? ' (all paths)' : ` ${site.paths.join(', ')}`
  return `${site.host}${paths} · ${site.methods.join(', ')}`
}

function sentAs(inject) {
  if (!inject) return 'Authorization: Bearer …'
  return inject.type === 'query'
    ? `?${inject.name}=…`
    : `${inject.name}: ${inject.prefix || ''}…`
}

/** Hosts in plain words: "a", "a and b", "a, b and c", "a, b and 2 more". */
export function hostList(hosts) {
  const unique = [...new Set(hosts)]
  if (unique.length <= 1) return unique[0] || ''
  if (unique.length <= 3) return `${unique.slice(0, -1).join(', ')} and ${unique[unique.length - 1]}`
  return `${unique.slice(0, 2).join(', ')} and ${unique.length - 2} more`
}

/**
 * Every host the key can be sent to right now: the sites for the owner and
 * agents, and each app's own sites unless that app is switched off.
 */
export function keyHosts(key) {
  const grants = key.grants || {}
  const sites = [
    ...(grants.sites || []),
    ...(grants.apps || []).filter(grant => !grant.paused).flatMap(grant => grant.sites || []),
  ]
  return [...new Set(sites.map(site => site.host))]
}

/** The line under a key's name on its page. */
export function sentToLine(key) {
  const hosts = keyHosts(key)
  return hosts.length ? `Sent only to ${hostList(hosts)}` : 'Not sent anywhere yet'
}

/** The overview row's subtitle: who uses the key, in a few words. */
export function usedByLine(key, names) {
  const grants = key.grants || {}
  const apps = grants.apps || []
  const active = apps.filter(grant => !grant.paused).map(grant => appLabel(names, grant.id))
  if (active.length) return `Used by ${active.join(', ')}${grants.agents ? ' · your agents' : ''}`
  if (grants.agents) return 'Used by your agents'
  const paused = apps.map(grant => appLabel(names, grant.id))
  return paused.length ? `Switched off for ${paused.join(', ')}` : 'Not used yet'
}

/**
 * When each user last used the key, from the usage log (newest first):
 * ``app:<id>`` per app and ``agent`` for all the owner's agents together.
 */
export function lastUseByCaller(usage) {
  const last = {}
  for (const entry of usage || []) {
    if (entry.outcome !== 'used' && entry.outcome !== 'revealed') continue
    const [kind] = String(entry.caller || '').split(':')
    const who = kind === 'agent' ? 'agent' : kind === 'app' ? entry.caller : null
    if (who && !last[who]) last[who] = entry.ts
  }
  return last
}

/**
 * The "Used by" rows: one per app with a switch that pauses it (paused apps
 * keep their sites), then one for the owner's agents.
 */
export function usedByRows(key, names, lastUse = {}, now = Date.now()) {
  const grants = key.grants || {}
  const apps = (grants.apps || []).map(grant => {
    const label = appLabel(names, grant.id)
    const used = lastUse[`app:${grant.id}`]
    return {
      id: grant.id,
      label,
      initial: label.trim().charAt(0).toUpperCase() || '?',
      on: !grant.paused,
      sub: grant.paused ? 'Switched off'
        : used ? `Last used ${relativeTime(used, now)}`
        : (grant.sites || []).length ? 'Can use it' : 'No websites yet',
    }
  })
  const agentsUsed = lastUse.agent
  const agents = {
    id: 'agents',
    label: 'Your agents',
    on: Boolean(grants.agents),
    sub: !grants.agents ? 'Switched off'
      : agentsUsed ? `Can use it and read it · last used ${relativeTime(agentsUsed, now)}`
      : 'Can use it and read it',
  }
  return { apps, agents }
}

/**
 * A name for a pasted key from its recognisable prefix, or null. Order
 * matters: ``sk-ant-`` is Anthropic's, any other ``sk-`` is OpenAI's.
 */
const KNOWN_PREFIXES = [
  [/^(ghp_|github_pat_)/, 'GitHub'],
  [/^sk-ant-/, 'Anthropic'],
  [/^(sk-proj-|sk-)/, 'OpenAI'],
  [/^AIza/, 'Google'],
  [/^(xoxb-|xoxp-)/, 'Slack'],
  [/^AKIA/, 'AWS'],
  [/^glpat-/, 'GitLab'],
]

export function serviceForValue(value) {
  const text = String(value || '').trim()
  return KNOWN_PREFIXES.find(([pattern]) => pattern.test(text))?.[1] || null
}

/** ``base`` unless a key already has that name (in any case), then base-2, base-3... */
export function freeKeyName(base, existingNames) {
  const taken = new Set((existingNames || []).map(name => name.toLowerCase()))
  if (!taken.has(base.toLowerCase())) return base
  let n = 2
  while (taken.has(`${base}-${n}`.toLowerCase())) n += 1
  return `${base}-${n}`
}

/**
 * The name a new key is saved under: the owner's own if typed, else one from
 * the value's prefix, else a generic "key"; a chosen-for-you name never takes
 * an existing key's. ``taken`` marks a typed name that already exists.
 */
export function newKeyName(typed, value, existingNames) {
  const own = String(typed || '').trim()
  if (own) {
    const taken = (existingNames || []).some(name => name.toLowerCase() === own.toLowerCase())
    return { name: own, service: null, valid: NAME_PATTERN.test(own), taken }
  }
  const service = serviceForValue(value)
  return { name: freeKeyName(service || 'key', existingNames), service, valid: true, taken: false }
}

/**
 * Each pending request gets its own form, and so its own typed value. A request
 * replacing another (an app that reused a removed app's id) keeps the id, the
 * app and key, but not the ticket, so nothing typed for the old one is sent.
 */
export function requestFormKey(request) {
  return `${request.id}:${request.ticket}`
}

// A burst of asks (one app may hold ten) must not bury the keys themselves.
const REQUESTS_SHOWN = 3

export function shownRequests(pending, showAll) {
  return showAll ? pending : pending.slice(0, REQUESTS_SHOWN)
}

/** Who asked: the owner's agent, or the installed app (told apart from a namesake). */
export function requestAppName(request, names) {
  if (request.requester === 'agent') return 'Your agent'
  return names[request.app] || request.app_name || `App ${request.app}`
}

/**
 * What the request asks for. A key another app saved for itself is not the
 * owner's (``savedBy`` names that app), also when this app already uses it
 * and asks for more sites: allowing sends this app's requests with that
 * app's value, which that app can still change.
 */
export function requestSubject(request, names) {
  if (request.requester === 'agent') return { kind: 'agent' }
  const origin = request.key_exists ? request.key_origin : null
  const savedBy = origin && origin.app !== request.app ? appLabel(names, origin.app) : null
  if (request.granted) return savedBy ? { kind: 'more-sites', savedBy } : { kind: 'more-sites' }
  if (!request.key_exists) return { kind: 'new' }
  if (savedBy) return { kind: 'saved-by-other', savedBy }
  return { kind: 'yours' }
}

const strong = text => ({ text, strong: true })

/**
 * The card's words for one request: ``lead`` is a list of segments (bold ones
 * name who, which key and where), ``note`` says where the key may go, and
 * ``warning`` with ``allowHidden`` keep another app's key behind an explicit
 * "anyway" so the safe answer is the default.
 */
export function requestWording(request, names) {
  const subject = requestSubject(request, names)
  const who = requestAppName(request, names)
  const hosts = hostList((request.sites || []).map(site => site.host))
  const key = request.key
  const needsValue = !request.key_exists
  const appNote = hosts
    ? `It will only ever be sent to ${hosts}. ${who} never sees the key itself.`
    : `It names no websites, so ${who} can't send this key anywhere until you add one on the key's page.`
  const base = { kind: subject.kind, who, needsValue, savedBy: subject.savedBy || null, allowHidden: false, warning: null }
  const otherApp = subject.savedBy && {
    allowHidden: true,
    warning: `This key belongs to ${subject.savedBy}, not you: ${subject.savedBy} saved it for itself and can change it at any time. `
      + `Allowing sends ${who}'s requests with ${subject.savedBy}'s key. To use your own, choose Not now and add a key of yours.`,
  }
  switch (subject.kind) {
    case 'agent':
      return {
        ...base,
        lead: hosts
          ? [strong(who), ' wants to send ', strong(key), ' to ', strong(hosts)]
          : [strong(who), ' wants to use ', strong(key)],
        note: needsValue
          ? 'Paste the key to save it. Your agents will be able to read it, and send it only to websites you allow.'
          : 'Allowing lets your agents read it and send it there.',
      }
    case 'new':
      return { ...base, lead: [strong(who), ' needs a key it calls ', strong(key)], note: appNote }
    case 'more-sites':
      return {
        ...base,
        ...otherApp,
        lead: hosts
          ? [strong(who), ' wants to use ', strong(key), ' on more websites: ', strong(hosts)]
          : [strong(who), ' wants to use ', strong(key), ' on more websites'],
        note: `${who} never sees the key itself.`,
      }
    case 'saved-by-other':
      return {
        ...base,
        ...otherApp,
        lead: [strong(who), ' wants to use ', strong(key), ', a key ', strong(subject.savedBy), ' saved'],
        note: appNote,
      }
    default:
      return { ...base, lead: [strong(who), ' needs your ', strong(key), ' key'], note: appNote }
  }
}

/** One line of Recent activity as a sentence, or null for entries the owner needn't read. */
export function usageSentence(entry, names, now = Date.now()) {
  const [kind, id] = String(entry.caller || '').split(':')
  const who = kind === 'app' ? appLabel(names, Number(id))
    : kind === 'agent' ? 'Your agent'
    : kind === 'platform' ? 'Möbius'
    : kind === 'owner' ? 'You' : entry.caller
  const outcome = String(entry.outcome || '')
  const when = entry.ts ? ` ${relativeTime(entry.ts, now)}` : ''
  const subjectApp = () => appLabel(names, Number(outcome.split(':')[1]?.split(' ')[0]))
  let text = null
  if (outcome === 'used') text = `${who} used it`
  else if (outcome === 'revealed') text = `${who} read it`
  else if (outcome === 'requested') text = `${who} asked to use it`
  else if (outcome === 'failed') text = `${who} couldn't reach the website`
  else if (outcome === 'denied' && /paused/.test(entry.reason || '')) text = `${who} was refused (switched off)`
  else if (outcome === 'denied') text = entry.host === '-' ? `${who} tried to read it` : `${who} was refused`
  else if (outcome === 'added') text = `${who} added it`
  else if (outcome === 'updated') text = `${who} changed it`
  else if (outcome === 'released') text = `${who} gave it up`
  else if (outcome.startsWith('revoked app:')) text = `${subjectApp()} lost it (app deleted)`
  else if (outcome.startsWith('granted app:')) text = `${who} let ${subjectApp()} use it`
  else if (outcome.startsWith('denied app:')) text = `${who} said no to ${subjectApp()}`
  else if (outcome.startsWith('forgot request app:')) text = `${who} let ${subjectApp()} ask again`
  else if (outcome.startsWith('granted agent')) text = `${who} let your agents use it`
  else if (outcome.startsWith('denied agent')) text = `${who} said no to your agent`
  else if (outcome.startsWith('forgot request agent')) text = `${who} let your agent ask again`
  return text && `${text}${when}`
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
          request={request}
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
