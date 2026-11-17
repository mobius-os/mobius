/*
 * Settings › Keys in words: what each request card, row and activity line
 * says, and how a new key is named. Pure and unit-tested; KeysSection.jsx
 * renders it.
 */

const NAME_PATTERN = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/

/**
 * Two installed apps may share a name; a grant or request must still say
 * which one it is, so a shared name carries the app's number.
 */
export function distinctAppNames(apps) {
  const counts = {}
  for (const app of apps) counts[app.name] = (counts[app.name] || 0) + 1
  return apps.map(app => (counts[app.name] > 1 ? { ...app, name: `${app.name} (#${app.id})` } : app))
}

export function appLabel(names, id) {
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

export function sentAs(inject) {
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
export const REQUESTS_SHOWN = 3

export function shownRequests(pending, showAll) {
  return showAll ? pending : pending.slice(0, REQUESTS_SHOWN)
}

/** Who asked: the owner's agent, or the installed app (told apart from a namesake). */
export function requestAppName(request, names) {
  if (request.requester === 'agent') return 'Your agent'
  return names[request.app] || request.app_name || `App ${request.app}`
}

/** A switched-off app asking for more websites stays off after Allow; its card says so. */
export function requestAppPaused(keys, request) {
  return Boolean(((keys || []).find(key => key.name === request.key)?.grants?.apps || [])
    .find(grant => grant.id === request.app)?.paused)
}

/** Who a request speaks for, mid-sentence: "your agent" or the app's name. */
function askerInSentence(request, names) {
  return request.requester === 'agent' ? 'your agent' : requestAppName(request, names)
}

/**
 * The owner's answer as one sentence: ``kind`` is ``'approve'`` or
 * ``'deny'``. ``message`` is what the chat card posts as the owner's own
 * message so the asking agent resumes ("Allowed: your agent may send maps to
 * tiles.example.com."); ``line`` is what the answered card then says. Both
 * name the key and its websites, never a value.
 */
export function keyAnswerWords(request, kind, names) {
  const who = askerInSentence(request, names)
  const hosts = hostList((request.sites || []).map(site => site.host))
  const key = request.key
  if (kind === 'approve') {
    return hosts
      ? { message: `Allowed: ${who} may send ${key} to ${hosts}.`, line: `You allowed ${who} to send ${key} to ${hosts}.` }
      : { message: `Allowed: ${who} may use ${key}.`, line: `You allowed ${who} to use ${key}.` }
  }
  return hosts
    ? { message: `Not now: ${who} may not send ${key} to ${hosts}.`, line: `You said no to ${who} sending ${key} to ${hosts}.` }
    : { message: `Not now: ${who} may not use ${key}.`, line: `You said no to ${who} using ${key}.` }
}

/**
 * What the request asks for. A key another app saved for itself is not the
 * owner's (``savedBy`` names that app), also when this app already uses it
 * and asks for more sites: allowing sends this app's requests with that
 * app's value, which that app can still change.
 */
export function requestSubject(request, names) {
  const origin = request.key_exists ? request.key_origin : null
  // An agent is never the app that saved a key, so any origin is another app's.
  const savedBy = origin && (request.requester === 'agent' || origin.app !== request.app)
    ? appLabel(names, origin.app) : null
  if (request.requester === 'agent') return savedBy ? { kind: 'agent', savedBy } : { kind: 'agent' }
  if (request.granted) return savedBy ? { kind: 'more-sites', savedBy } : { kind: 'more-sites' }
  if (!request.key_exists) return { kind: 'new' }
  if (savedBy) return { kind: 'saved-by-other', savedBy }
  return { kind: 'yours' }
}

const strong = text => ({ text, strong: true })

/**
 * The card's words for one request. ``lead`` is the question it asks, as a
 * list of segments (bold ones name who, which key and where); ``says``
 * labels the requester's own reason; ``note`` says where the key may go. A
 * new key is asked in two steps: first the question, then ``pasteTitle``
 * and ``pasteNote`` over the paste field. ``warning`` with ``allowHidden``
 * keep another app's key behind an explicit "anyway" so the safe answer is
 * the default.
 */
export function requestWording(request, names) {
  const subject = requestSubject(request, names)
  const who = requestAppName(request, names)
  const hosts = hostList((request.sites || []).map(site => site.host))
  const key = request.key
  const needsValue = !request.key_exists
  // "Let your agent …", but "Your agent says:".
  const asked = subject.kind === 'agent' ? 'your agent' : who
  const on = hosts ? [' on ', strong(hosts)] : []
  const appNote = hosts
    ? `It will only ever be sent to ${hosts}. ${who} never sees the key itself.`
    : `It names no websites, so ${who} can't send this key anywhere until you add one on the key's page.`
  const base = {
    kind: subject.kind, who, needsValue, savedBy: subject.savedBy || null, allowHidden: false, warning: null,
    says: `${who} says:`,
    pasteTitle: hosts ? `Paste your ${hosts} key` : 'Paste the key',
    pasteNote: `Saved as “${key}”. You'll find it in Settings › Keys.`,
  }
  const otherApp = subject.savedBy && {
    allowHidden: true,
    warning: `This key belongs to ${subject.savedBy}, not you: ${subject.savedBy} saved it for itself and can change it at any time. `
      + (subject.kind === 'agent'
        ? `Allowing lets your agents send ${subject.savedBy}'s key. To use your own, choose Not now and add a key of yours.`
        : `Allowing sends ${who}'s requests with ${subject.savedBy}'s key. To use your own, choose Not now and add a key of yours.`),
  }
  // A switched-off app stays off: allowing only adds the websites it asked for.
  const stillOff = request.app_paused
    ? ` ${who} is switched off for this key and stays off until you switch it back on under Used by.` : ''
  switch (subject.kind) {
    case 'agent':
      return {
        ...base,
        ...otherApp,
        lead: hosts
          ? ['Let ', strong(asked), ' send ', strong(key), ' to ', strong(hosts), '?']
          : ['Let ', strong(asked), ' use ', strong(key), '?'],
        note: needsValue
          ? 'You have no key by that name yet. Your agents will be able to read it, and send it only to websites you allow.'
          : hosts ? `Allowing lets your agents read it and send it to ${hosts}.` : 'Allowing lets your agents read and use it.',
      }
    case 'new':
      return { ...base, lead: ['Let ', strong(who), ' use a key', ...on, '?'], note: appNote }
    case 'more-sites':
      return {
        ...base,
        ...otherApp,
        lead: hosts
          ? ['Let ', strong(who), ' use ', strong(key), ' on more websites: ', strong(hosts), '?']
          : ['Let ', strong(who), ' use ', strong(key), ' on more websites?'],
        note: `${who} never sees the key itself.${stillOff}`,
      }
    case 'saved-by-other':
      return {
        ...base,
        ...otherApp,
        lead: ['Let ', strong(who), ' use ', strong(key), ', a key ', strong(subject.savedBy), ' saved?'],
        note: appNote,
      }
    default:
      return { ...base, lead: ['Let ', strong(who), ' use your ', strong(key), ' key', ...on, '?'], note: appNote }
  }
}

/**
 * What happened in one Recent activity entry ("Weather used it"), or null for
 * entries the owner needn't read. The row shows when beside it.
 */
export function usageWhat(entry, names) {
  const [kind, id] = String(entry.caller || '').split(':')
  const who = kind === 'app' ? appLabel(names, Number(id))
    : kind === 'agent' ? 'Your agent'
    : kind === 'platform' ? 'Möbius'
    : kind === 'owner' ? 'You' : entry.caller
  const outcome = String(entry.outcome || '')
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
  return text
}
