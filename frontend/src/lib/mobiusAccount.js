/* Owner-authenticated client for the local Möbius account bridge (/api/identity): validated identity, model access, Railway inventory and the presentation rules Settings › Möbius shows them with. */
import { BASE, getAuthHeaders } from '../api/client.js'

// Identity bridge requests are owner actions, so they use the owner bearer
// directly. Every response the page renders passes through a parser here:
// unknown fields are dropped (a newer account service cannot blank the page),
// while the fields shown, and the rules that keep signed-out or unavailable
// accounts from exposing account data, are validated strictly.

export class MobiusAccessError extends Error {
  constructor(message, status = 0, code = '', confirmUrl = '') {
    super(message)
    this.name = 'MobiusAccessError'
    this.status = status
    this.code = code
    this.confirmUrl = confirmUrl
  }
}

// The platform refuses deployment creation, deletion, resizing, Recovery and
// Railway disconnect with this code plus the owner's account dashboard. Möbius
// never performs those itself; it only links there.
export const ACCOUNT_CONFIRM_CODE = 'confirm_on_account'

const HANDLE = /^[a-z0-9_]{3,30}$/
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const RAILWAY_INSTANCE_ID = /^mob_[A-Za-z0-9_-]{3,80}$/
const ACCOUNT_MODES = ['signed_out', 'linked', 'managed']
const RAILWAY_ACCESS = ['signed_out', 'reconnect', 'unavailable', 'available']
const AGENT_ACCESS = ['signed_out', 'unavailable', 'available']
const TRACKED_DEPLOYMENT_STATUSES = new Set(['queued', 'creating', 'deploying', 'deleting'])
const TRACKED_UPDATE_STATES = new Set(['pending', 'checking', 'retry'])
const BUILDING_STATUSES = new Set(['queued', 'creating', 'deploying'])
const DELETE_CONFIRMATION_FALLBACK = (
  "Möbius couldn't confirm whether Railway removed this project. "
  + 'Try deleting again, or open Railway to check.'
)

export const HANDLE_PATTERN = HANDLE
export const AVATAR_TYPES = ['image/jpeg', 'image/png', 'image/webp']
export const AVATAR_MAX_BYTES = 5 * 1024 * 1024

function isObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function boundedText(value, min, max) {
  return typeof value === 'string' && value.length >= min && value.length <= max
}

function nullableText(value, max) {
  return value === null || value === undefined || (typeof value === 'string' && value.length <= max)
}

function loopback(hostname) {
  const host = hostname.toLowerCase()
  return host === 'localhost'
    || host.endsWith('.localhost')
    || host === '[::1]'
    || /^127(?:\.\d{1,3}){3}$/.test(host)
}

/** Parse an https (or, unless httpsOnly, loopback http) URL without credentials; null otherwise. */
export function webUrl(value, { httpsOnly = false } = {}) {
  if (typeof value !== 'string' || value.length > 2048) return null
  let parsed
  try {
    parsed = new URL(value)
  } catch {
    return null
  }
  if (parsed.username || parsed.password) return null
  if (parsed.protocol === 'https:') return parsed
  if (!httpsOnly && parsed.protocol === 'http:' && loopback(parsed.hostname)) return parsed
  return null
}

/** The account dashboard address, accepted only as a plain https …/dashboard URL. */
export function dashboardUrl(value) {
  const url = webUrl(value, { httpsOnly: true })
  return url && !url.search && !url.hash && /\/dashboard\/?$/.test(url.pathname) ? url.href : ''
}

function calendarDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const [year, month, day] = value.split('-').map(Number)
  const parsed = new Date(Date.UTC(year, month - 1, day))
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day
}

export function formatMembershipMonth(value, locales) {
  if (!calendarDate(value)) return null
  return new Intl.DateTimeFormat(locales, { month: 'short', year: 'numeric', timeZone: 'UTC' })
    .format(new Date(`${value}T00:00:00Z`))
}

// ── Identity ──────────────────────────────────────────────────────────────

function parseProfile(profile, { degraded = false, nullableEmail = false } = {}) {
  if (!isObject(profile)) return null
  const emailOk = (nullableEmail && profile.email === null)
    || (boundedText(profile.email, 3, 320) && EMAIL.test(profile.email))
  if (!boundedText(profile.user_id, 1, 128) || !emailOk) return null
  if (degraded) {
    // A managed outage keeps only the locally bound user and email.
    if (profile.handle != null || profile.avatar_url != null) return null
    return { user_id: profile.user_id, email: profile.email, handle: null, avatar_url: null }
  }
  if (profile.handle !== null && !(typeof profile.handle === 'string' && HANDLE.test(profile.handle))) return null
  if (profile.avatar_url !== null && !webUrl(profile.avatar_url, { httpsOnly: true })) return null
  // Provider-supplied display names are deliberately not kept: the handle is the identity.
  return {
    user_id: profile.user_id,
    email: profile.email,
    handle: profile.handle,
    avatar_url: profile.avatar_url,
  }
}

/**
 * This Möbius's own address, as the local bridge reports it (its configured
 * frontend origin). It is display-only and may be plain HTTP on any host,
 * which self-hosted installations support; addresses the account service
 * reports for other deployments keep webUrl's stricter rule.
 */
function localDeploymentUrl(value) {
  if (typeof value !== 'string' || value.length > 2048) return null
  let parsed
  try {
    parsed = new URL(value)
  } catch {
    return null
  }
  if (parsed.username || parsed.password) return null
  return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed : null
}

function parseDeployment(item) {
  if (
    !isObject(item)
    || !boundedText(item.id, 1, 96)
    || !boundedText(item.name, 1, 128)
    || !boundedText(item.status, 1, 64)
    || !nullableText(item.region, 64)
    || (item.current !== undefined && typeof item.current !== 'boolean')
    || !(item.current === true ? localDeploymentUrl(item.url) : webUrl(item.url))
  ) return null
  return {
    id: item.id,
    name: item.name,
    status: item.status,
    url: item.url,
    region: item.region || null,
    current: item.current === true,
  }
}

/** Validate GET /api/identity (and the identity returned by profile, avatar and link completion). */
export function parseIdentity(value) {
  if (
    !isObject(value)
    || !ACCOUNT_MODES.includes(value.account_mode)
    || typeof value.account_unavailable !== 'boolean'
    || !Array.isArray(value.deployments)
    || value.deployments.length < 1
    || value.deployments.length > 100
    || (value.member_since != null && !calendarDate(value.member_since))
  ) throw new Error('Möbius returned an invalid account response.')
  const deployments = value.deployments.map(parseDeployment)
  if (deployments.some(item => !item) || !deployments.some(item => item.current)) {
    throw new Error('Möbius returned an invalid account response.')
  }
  const base = {
    account_mode: value.account_mode,
    account_unavailable: value.account_unavailable,
    member_since: value.member_since || null,
    deployments,
  }

  if (value.account_mode === 'signed_out') {
    if (value.account_unavailable || value.instance_id != null || value.profile !== null) {
      throw new Error('Möbius returned account details while signed out.')
    }
    return { ...base, profile: null }
  }
  if (value.account_mode === 'linked') {
    if (value.instance_id != null) throw new Error('Möbius returned an invalid linked-account response.')
    if (value.account_unavailable) {
      // A linked outage never shows stale profile data.
      if (value.profile !== null) throw new Error('Möbius returned an invalid linked-account response.')
      return { ...base, profile: null }
    }
    const profile = parseProfile(value.profile)
    if (!profile) throw new Error('Möbius returned an invalid linked-account response.')
    return { ...base, profile }
  }
  const profile = boundedText(value.instance_id, 1, 256)
    ? parseProfile(value.profile, { degraded: value.account_unavailable, nullableEmail: true })
    : null
  if (!profile) throw new Error('Möbius returned an invalid managed-account response.')
  return { ...base, profile }
}

/** Plain-language account state for badges: 'signed_out' | 'linked' | 'managed' | 'unavailable'. */
export function accountState(identity) {
  if (!identity) return 'unavailable'
  if (identity.account_unavailable) return 'unavailable'
  return identity.account_mode
}

// ── Model access ──────────────────────────────────────────────────────────

function parsePricing(pricing) {
  if (!isObject(pricing)) return null
  const kinds = ['input', 'cached_input', 'output']
  if (!kinds.every(kind => Number.isFinite(pricing[kind]) && pricing[kind] >= 0)) return null
  return { input: pricing.input, cached_input: pricing.cached_input, output: pricing.output }
}

/**
 * Read GET /api/identity/agent into exactly the fields Settings renders.
 * Signed-out or unavailable access never exposes models, balance or trial.
 */
export function parseAgentAccess(value) {
  if (!isObject(value) || !AGENT_ACCESS.includes(value.agent_access)) {
    throw new Error('Möbius returned invalid model access.')
  }
  const models = value.models ?? []
  const balance = value.balance ?? {}
  const trial = value.trial ?? {}
  const retention = value.retention ?? {}
  if (!Array.isArray(models) || ![balance, trial, retention].every(isObject)) {
    throw new Error('Möbius returned invalid model access.')
  }
  if (value.agent_access !== 'available') {
    if (models.length || 'available_units' in balance || 'available_usd' in balance || 'state' in trial) {
      throw new Error('Möbius exposed model data without account access.')
    }
    return { agent_access: value.agent_access, models: [], balance: {}, trial: {}, retention: {} }
  }

  if (models.length > 50) throw new Error('Möbius returned invalid model prices.')
  const seen = new Set()
  const parsedModels = models.map(model => {
    const pricing = isObject(model) ? parsePricing(model.pricing) : null
    if (!pricing || !boundedText(model.id, 1, 80) || !boundedText(model.name, 1, 80)) {
      throw new Error('Möbius returned invalid model prices.')
    }
    if (seen.has(model.id)) throw new Error('Möbius returned duplicate model aliases.')
    seen.add(model.id)
    return { id: model.id, name: model.name, pricing }
  })
  if (!boundedText(trial.state, 1, 40)) throw new Error('Möbius returned invalid trial state.')
  if (!Number.isSafeInteger(balance.available_units) || balance.available_units < 0) {
    throw new Error('Möbius returned an invalid model balance.')
  }
  if (!boundedText(retention.notice, 1, 500)) {
    throw new Error('Möbius returned invalid model retention state.')
  }
  return {
    agent_access: 'available',
    models: parsedModels,
    balance: { available_units: balance.available_units },
    trial: { state: trial.state },
    retention: { notice: retention.notice },
  }
}

export function formatCredit(units) {
  return new Intl.NumberFormat(undefined, {
    style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2,
  }).format(units / 1_000_000)
}

export function formatTokenRate(value) {
  return new Intl.NumberFormat(undefined, {
    style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 4,
  }).format(value)
}

// ── Railway inventory ─────────────────────────────────────────────────────

function parseRailwayInstance(instance) {
  if (
    !isObject(instance)
    || typeof instance.id !== 'string'
    || !RAILWAY_INSTANCE_ID.test(instance.id)
    || !boundedText(instance.name, 1, 80)
    || !boundedText(instance.status, 1, 32)
    || !nullableText(instance.current_step, 160)
    || !nullableText(instance.last_error, 360)
    || (instance.url != null && !webUrl(instance.url))
  ) return null
  let railwayUrl = null
  if (instance.railway_url != null) {
    const parsed = webUrl(instance.railway_url, { httpsOnly: true })
    if (!parsed || parsed.hostname !== 'railway.com') return null
    railwayUrl = parsed.href
  }
  const resources = instance.resources
  if (
    !isObject(resources)
    || !nullableText(resources.cpu, 16)
    || (resources.memory_mb != null && !Number.isInteger(resources.memory_mb))
    || (resources.volume_size_mb != null && !Number.isInteger(resources.volume_size_mb))
  ) return null
  const actions = instance.actions
  if (!isObject(actions) || !['edit_resources', 'retry', 'delete'].every(key => typeof actions[key] === 'boolean')) {
    return null
  }
  const updates = isObject(instance.updates) && boundedText(instance.updates.state, 0, 40)
    ? { state: instance.updates.state }
    : null
  return {
    id: instance.id,
    name: instance.name,
    status: instance.status,
    url: instance.url || null,
    railway_url: railwayUrl,
    current_step: instance.current_step || null,
    last_error: instance.last_error || null,
    resources: {
      cpu: resources.cpu || null,
      memory_mb: resources.memory_mb ?? null,
      volume_size_mb: resources.volume_size_mb ?? null,
    },
    actions: {
      edit_resources: actions.edit_resources,
      retry: actions.retry,
      delete: actions.delete,
      // Recovery is offered unless the account service explicitly withholds it.
      recover: actions.recover !== false,
    },
    updates,
  }
}

/** Validate GET /api/identity/railway?dashboard=1. */
export function parseRailway(value) {
  if (
    !isObject(value)
    || !RAILWAY_ACCESS.includes(value.railway_access)
    || !Array.isArray(value.instances)
    || value.instances.length > 100
  ) throw new Error('Möbius returned invalid Railway deployment state.')
  const instances = value.instances.map(parseRailwayInstance)
  if (instances.some(item => !item)) throw new Error('Möbius returned invalid Railway deployment state.')
  // Older platforms omit dashboard_url; protected actions then stay unavailable
  // with an update explanation rather than guessing an account origin.
  const dashboard = dashboardUrl(value.dashboard_url) || ''
  if (value.railway_access !== 'available') {
    if (value.connection != null || instances.length) {
      throw new Error('Möbius exposed Railway details without account access.')
    }
    return { railway_access: value.railway_access, connection: null, instances: [], dashboard_url: dashboard }
  }
  let connection = null
  if (value.connection != null) {
    if (
      !isObject(value.connection)
      || typeof value.connection.connected !== 'boolean'
      || !nullableText(value.connection.account, 320)
    ) throw new Error('Möbius returned an invalid Railway connection.')
    connection = { connected: value.connection.connected, account: value.connection.account || '' }
  }
  return { railway_access: 'available', connection, instances, dashboard_url: dashboard }
}

export function railwayAccountChanged(previousAccount, next) {
  return Boolean(
    next?.connection?.connected
    && next.connection.account
    && next.connection.account !== previousAccount,
  )
}

const lower = value => String(value || '').toLowerCase()

export function deploymentNeedsTracking(instance) {
  return TRACKED_DEPLOYMENT_STATUSES.has(lower(instance?.status))
    || TRACKED_UPDATE_STATES.has(lower(instance?.updates?.state))
}

export function deploymentIsBuilding(instance) {
  return BUILDING_STATUSES.has(lower(instance?.status))
}

export function deploymentCanOpen(instance) {
  return ['ready', 'active'].includes(lower(instance?.status))
}

export function deploymentCanRecover(instance) {
  return instance?.actions?.recover !== false && ['ready', 'error'].includes(lower(instance?.status))
}

/** Status label, explanatory detail and tone ('success' | 'progress' | 'danger' | 'muted') for one deployment. */
export function deploymentPresentation(instance) {
  const status = lower(instance?.status)
  const step = String(instance?.current_step || '').trim()
  const error = String(instance?.last_error || '').trim()
  if (status === 'ready' || status === 'active') {
    return { label: 'Active', detail: step && step.toLowerCase() !== 'ready' ? step : '', tone: 'success' }
  }
  if (status === 'deleting') {
    return { label: 'Deleting', detail: 'Railway is removing this project. This page updates automatically.', tone: 'progress' }
  }
  if (status === 'delete_failed') {
    return {
      label: 'Deletion needs attention',
      detail: error && !/check the build/i.test(error) ? error : DELETE_CONFIRMATION_FALLBACK,
      tone: 'danger',
    }
  }
  if (deploymentIsBuilding(instance)) {
    return { label: 'Deploying', detail: step || 'Möbius is following the Railway build.', tone: 'progress' }
  }
  if (status === 'error') {
    return { label: 'Needs attention', detail: error || step || 'This deployment needs your attention.', tone: 'danger' }
  }
  if (status === 'linked') return { label: 'Linked', detail: error, tone: 'muted' }
  return { label: step || instance?.status || 'Status unavailable', detail: error, tone: 'muted' }
}

function deploymentOrigin(value) {
  const parsed = webUrl(value, { httpsOnly: true })
  return parsed ? parsed.origin : ''
}

/**
 * One card per deployment: the bridge's deployments (this one first), each
 * matched to its Railway instance by id or https origin, then any Railway
 * instance the bridge did not list.
 */
export function mergeDeployments(items, railwayInstances = []) {
  const byId = new Map(railwayInstances.map(item => [item.id, item]))
  const byOrigin = new Map(railwayInstances
    .map(item => [deploymentOrigin(item.url), item])
    .filter(([origin]) => origin))
  const cards = [...items]
    .sort((a, b) => Number(b.current) - Number(a.current))
    .map(item => ({ ...item, railway: byId.get(item.id) || byOrigin.get(deploymentOrigin(item.url)) || null }))
  const claimed = new Set(cards.map(card => card.railway?.id).filter(Boolean))
  for (const instance of railwayInstances) {
    if (claimed.has(instance.id)) continue
    cards.push({
      id: instance.id,
      name: instance.name,
      status: instance.status,
      url: instance.url,
      region: null,
      current: false,
      railway: instance,
    })
  }
  return cards
}

export function resourceSummary(resources) {
  return [
    resources.cpu ? `${resources.cpu} vCPU` : 'Plan CPU limit',
    resources.memory_mb ? `${resources.memory_mb} MB RAM` : 'Plan RAM limit',
    resources.volume_size_mb ? `${resources.volume_size_mb} MB storage` : '',
  ].filter(Boolean).join(' · ')
}

function shortText(value, max) {
  return typeof value === 'string' && value.length <= max ? value : ''
}

/** Display-only live metrics. Cost is kept only with a heading: unknown cost is hidden, never zero. */
export function parseDeploymentMetrics(value) {
  if (!isObject(value)) throw new Error('Möbius returned invalid deployment metrics.')
  const section = (name, keys) => {
    const raw = isObject(value[name]) ? value[name] : {}
    return Object.fromEntries(keys.map(key => [key, shortText(raw[key], 80)]))
  }
  const cost = isObject(value.cost) && boundedText(value.cost.heading, 1, 200)
    ? { heading: value.cost.heading, note: shortText(value.cost.note, 360), percent: shortText(value.cost.percent, 40) }
    : null
  return {
    runtime: section('runtime', ['status_label', 'region_label', 'latest_deployment_at', 'data_status']),
    cpu: section('cpu', ['label', 'limit_label', 'percent']),
    memory: section('memory', ['label', 'limit_label', 'percent']),
    volume: section('volume', ['used_label', 'allocated_label', 'percent']),
    network: section('network', ['rx_label', 'tx_label', 'percent']),
    cost,
  }
}

export function meterPercent(value) {
  const parsed = parseFloat(String(value ?? '').replace('%', ''))
  return Number.isFinite(parsed) ? Math.max(0, Math.min(100, parsed)) : 0
}

// ── Requests ──────────────────────────────────────────────────────────────

const PARSERS = {
  '/api/identity': parseIdentity,
  '/api/identity/profile': parseIdentity,
  '/api/identity/avatar': parseIdentity,
  '/api/identity/link/complete': parseIdentity,
  '/api/identity/agent': parseAgentAccess,
  '/api/identity/agent/trial': parseAgentAccess,
  '/api/identity/railway': parseRailway,
}

/**
 * One owner-authenticated request to the local account bridge. Rejects with a
 * MobiusAccessError carrying the HTTP status (0 for a transport failure), the
 * service's error code and, for dashboard-only actions, the validated
 * dashboard address. Known routes return their validated shape.
 */
export async function mobiusAccessRequest(path, options = {}) {
  let response
  try {
    response = await fetch(`${BASE}${path}`, { ...options, headers: getAuthHeaders(options.headers) })
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw new MobiusAccessError('This Möbius could not reach its account service. Check your connection and try again.')
  }
  if (response.status === 204) return null
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = body?.detail
    const confirmUrl = detail?.code === ACCOUNT_CONFIRM_CODE ? dashboardUrl(detail.confirm_url) : ''
    throw new MobiusAccessError(
      typeof detail === 'string' ? detail : detail?.message || 'Your Möbius account is unavailable right now.',
      response.status,
      typeof detail?.code === 'string' ? detail.code : '',
      confirmUrl,
    )
  }
  const parse = PARSERS[path.split(/[?#]/, 1)[0]]
  return parse ? parse(body) : body
}

/** A request that failed in a way a retry can fix (no response, or a server-side failure). */
export function retryableAccessError(error) {
  return error instanceof MobiusAccessError && (error.status === 0 || error.status >= 500)
}
