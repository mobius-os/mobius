export const ACCOUNT_MODES = Object.freeze(['signed_out', 'linked', 'managed'])
export const RAILWAY_ACCESS = Object.freeze([
  'signed_out',
  'reconnect',
  'unavailable',
  'available',
])
export const DELETION_STATES = Object.freeze([
  'present',
  'missing',
  'missing_unconfirmed',
  'authorization',
  'unknown',
])

const ACCOUNT_LINK_WINDOW_MS = 10 * 60 * 1000
const BROKER_ACK_WINDOW_MS = 5 * 1000
const ACCOUNT_LINK_STATE = /^[A-Za-z0-9_-]{32,512}$/
const ACCOUNT_LINK_ATTEMPT = /^[A-Za-z0-9_-]{16,512}$/
const ACCOUNT_LINK_CODE = /^[A-Za-z0-9_-]{32,512}$/
const HANDLE = /^[a-z0-9_]{3,30}$/
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const TRACKED_DEPLOYMENT_STATUSES = new Set([
  'queued',
  'creating',
  'deploying',
  'deleting',
])
const TRACKED_UPDATE_STATES = new Set(['pending', 'checking', 'retry'])
export const IMAGE_UPDATE_POLICIES = Object.freeze(['automatic', 'manual'])
export const RAILWAY_REGION_IDS = Object.freeze([
  'us-west2', 'us-east4-eqdc4a', 'europe-west4-drams3a', 'asia-southeast1-eqsg3a',
])

// This is deliberately approximate, not browser geolocation. Unknown time
// zones leave Railway's own preferred region in charge.
export function suggestRailwayRegion(zone, offsetMinutes) {
  if (typeof zone !== 'string' || !Number.isFinite(offsetMinutes)) return ''
  if (zone.startsWith('America/')) {
    return offsetMinutes <= -390 ? 'us-west2' : 'us-east4-eqdc4a'
  }
  if (/^(Europe|Africa|Atlantic)\//.test(zone)) return 'europe-west4-drams3a'
  if (zone.startsWith('Asia/')) {
    return offsetMinutes >= 300 ? 'asia-southeast1-eqsg3a' : 'europe-west4-drams3a'
  }
  if (/^(Australia|Indian)\//.test(zone)) return 'asia-southeast1-eqsg3a'
  if (zone.startsWith('Pacific/')) {
    return offsetMinutes < 0 ? 'us-west2' : 'asia-southeast1-eqsg3a'
  }
  return ''
}

const DELETE_CONFIRMATION_FALLBACK = (
  "Möbius couldn't confirm whether Railway removed this project. "
  + 'Try deleting again, or open Railway to check.'
)

export class IdentityRequestError extends Error {
  constructor(message, status = 0, code = '') {
    super(message)
    this.name = 'IdentityRequestError'
    this.status = status
    this.code = code
  }
}

function exactKeys(value, required, optional = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const allowed = new Set([...required, ...optional])
  const keys = Object.keys(value)
  return required.every(key => keys.includes(key))
    && keys.every(key => allowed.has(key))
}

function loopback(hostname) {
  const host = hostname.toLowerCase()
  return host === 'localhost'
    || host.endsWith('.localhost')
    || host === '[::1]'
    || /^127(?:\.\d{1,3}){3}$/.test(host)
}

function webUrl(value, { httpsOnly = false } = {}) {
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
  return new Intl.DateTimeFormat(locales, {
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(`${value}T00:00:00Z`))
}

function validProfile(profile, { degraded = false, nullableEmail = false } = {}) {
  const fields = ['user_id', 'email', 'display_name', 'handle', 'avatar_url']
  if (!exactKeys(profile, fields)) return false
  const validEmail = (nullableEmail && profile.email === null) || (
    typeof profile.email === 'string'
    && profile.email.length <= 320
    && EMAIL.test(profile.email)
  )
  if (
    typeof profile.user_id !== 'string'
    || profile.user_id.length < 1
    || profile.user_id.length > 128
    || !validEmail
  ) return false
  if (degraded) {
    return profile.display_name === null
      && profile.handle === null
      && profile.avatar_url === null
  }
  return typeof profile.display_name === 'string'
    && profile.display_name.length <= 80
    && (profile.handle === null || HANDLE.test(profile.handle))
    && (profile.avatar_url === null || Boolean(webUrl(profile.avatar_url, { httpsOnly: true })))
}

function validDeployment(deployment) {
  const required = ['id', 'name', 'status', 'url']
  if (!exactKeys(deployment, required, ['region', 'current'])) return false
  return typeof deployment.id === 'string'
    && deployment.id.length > 0
    && deployment.id.length <= 96
    && typeof deployment.name === 'string'
    && deployment.name.length > 0
    && deployment.name.length <= 128
    && typeof deployment.status === 'string'
    && deployment.status.length > 0
    && deployment.status.length <= 64
    && (deployment.region === undefined
      || deployment.region === null
      || (typeof deployment.region === 'string' && deployment.region.length <= 64))
    && (deployment.current === undefined || typeof deployment.current === 'boolean')
    && Boolean(webUrl(deployment.url))
}

export function parseIdentity(value) {
  const fields = [
    'account_mode',
    'account_unavailable',
    'instance_id',
    'profile',
    'deployments',
  ]
  const validMemberSince = value?.member_since === null
    || calendarDate(value?.member_since)
  if (
    !exactKeys(value, [...fields, 'member_since'])
    || !validMemberSince
    || !ACCOUNT_MODES.includes(value.account_mode)
    || typeof value.account_unavailable !== 'boolean'
    || !Array.isArray(value.deployments)
    || value.deployments.length < 1
    || value.deployments.length > 100
    || !value.deployments.every(validDeployment)
    || !value.deployments.some(item => item.current === true)
  ) {
    throw new Error('Möbius returned an invalid identity response.')
  }

  if (value.account_mode === 'signed_out') {
    if (value.account_unavailable || value.instance_id !== null || value.profile !== null) {
      throw new Error('Möbius returned account details while signed out.')
    }
    return value
  }

  if (value.account_mode === 'linked') {
    const valid = value.instance_id === null
      && (value.account_unavailable
        ? value.profile === null
        : validProfile(value.profile))
    if (!valid) throw new Error('Möbius returned an invalid linked-account response.')
    return value
  }

  const validManaged = typeof value.instance_id === 'string'
    && value.instance_id.length > 0
    && (value.account_unavailable
      ? validProfile(value.profile, { degraded: true, nullableEmail: true })
      : validProfile(value.profile, { nullableEmail: true }))
  if (!validManaged) {
    throw new Error('Möbius returned an invalid managed-account response.')
  }
  return value
}

export function parseAgentAccess(value) {
  const accessStates = ['signed_out', 'unavailable', 'available']
  if (
    !exactKeys(value, ['agent_access', 'models', 'balance', 'trial', 'retention'])
    || !accessStates.includes(value.agent_access)
    || !Array.isArray(value.models)
    || value.models.length > 50
    || !value.balance || typeof value.balance !== 'object' || Array.isArray(value.balance)
    || !value.trial || typeof value.trial !== 'object' || Array.isArray(value.trial)
    || !value.retention || typeof value.retention !== 'object' || Array.isArray(value.retention)
  ) throw new Error('Möbius returned invalid model access.')
  if (value.agent_access !== 'available') {
    if (
      value.models.length
      || [value.balance, value.trial, value.retention]
        .some(section => Object.keys(section).length)
    ) throw new Error('Möbius exposed model data without account access.')
    return value
  }
  const modelIds = new Set()
  for (const model of value.models) {
    const pricing = model?.pricing
    if (
      !exactKeys(model, ['id', 'name', 'pricing'], ['context_window'])
      || typeof model?.id !== 'string'
      || !/^[a-z0-9][a-z0-9_-]{0,79}$/.test(model.id)
      || typeof model?.name !== 'string'
      || !model.name
      || model.name.length > 80
      || !pricing || typeof pricing !== 'object' || Array.isArray(pricing)
      || !exactKeys(pricing, ['input', 'cached_input', 'output'])
      || !['input', 'cached_input', 'output'].every(kind => (
        Number.isFinite(pricing[kind]) && pricing[kind] >= 0
      ))
      || (model.context_window !== undefined && (
        !Number.isSafeInteger(model.context_window) || model.context_window < 1
      ))
    ) throw new Error('Möbius returned invalid model prices.')
    if (modelIds.has(model.id)) throw new Error('Möbius returned duplicate model aliases.')
    modelIds.add(model.id)
  }
  const trialState = value.trial.state
  if (
    !exactKeys(value.trial, ['state'])
    || !['ready', 'active', 'expired', 'ineligible'].includes(trialState)
  ) {
    throw new Error('Möbius returned invalid trial state.')
  }
  if (
    !exactKeys(value.balance, ['available_units'], ['available_usd'])
    || !Number.isSafeInteger(value.balance.available_units)
    || value.balance.available_units < 0
    || (value.balance.available_usd !== undefined && (
      typeof value.balance.available_usd !== 'string'
      || !/^\d+(?:\.\d{1,6})?$/.test(value.balance.available_usd)
    ))
  ) throw new Error('Möbius returned an invalid model balance.')
  if (
    !exactKeys(value.retention, ['policy', 'notice'])
    || value.retention.policy !== 'local-testing-v1'
    || typeof value.retention.notice !== 'string'
    || value.retention.notice.length < 20
    || value.retention.notice.length > 500
  ) throw new Error('Möbius returned invalid model retention state.')
  return value
}

export function agentAccessPresentation(access) {
  const trialReady = access.trial.state === 'ready'
  const availableUnits = Number(access.balance.available_units || 0)
  return {
    needsActivation: trialReady,
    title: trialReady ? 'Start with $2 on us' : 'Model access is active',
    action: 'Activate $2 trial',
    showBalance: !trialReady,
    empty: !trialReady && availableUnits <= 0,
  }
}

function nullableString(value, max) {
  return value === null || (typeof value === 'string' && value.length <= max)
}

function validRailwayInstance(instance) {
  if (!exactKeys(instance, [
    'id', 'name', 'status', 'url', 'railway_url', 'current_step',
    'last_error', 'resources', 'actions',
  ], ['updates'])) return false
  if (
    typeof instance.id !== 'string'
    || !/^mob_[A-Za-z0-9_-]{3,80}$/.test(instance.id)
    || typeof instance.name !== 'string'
    || instance.name.length < 1
    || instance.name.length > 80
    || typeof instance.status !== 'string'
    || instance.status.length < 1
    || instance.status.length > 32
    || !nullableString(instance.current_step, 160)
    || !nullableString(instance.last_error, 360)
    || (instance.url !== null && !webUrl(instance.url))
  ) return false
  const railwayUrl = instance.railway_url === null
    ? null
    : webUrl(instance.railway_url, { httpsOnly: true })
  if (railwayUrl && railwayUrl.hostname !== 'railway.com') return false
  if (instance.railway_url !== null && !railwayUrl) return false
  const resources = instance.resources
  if (!exactKeys(resources, ['cpu', 'memory_mb', 'volume_size_mb', 'plan'])) return false
  if (
    !nullableString(resources.cpu, 16)
    || (resources.memory_mb !== null && !Number.isInteger(resources.memory_mb))
    || (resources.volume_size_mb !== null && !Number.isInteger(resources.volume_size_mb))
    || typeof resources.plan !== 'string'
    || resources.plan.length > 32
  ) return false
  const actions = instance.actions
  if (
    !exactKeys(actions, ['edit_resources', 'retry', 'delete'], ['edit_updates', 'recover'])
    || !Object.values(actions).every(value => typeof value === 'boolean')
  ) return false
  if (instance.updates === undefined) return true
  return exactKeys(instance.updates, ['policy', 'state', 'error'])
    && IMAGE_UPDATE_POLICIES.includes(instance.updates.policy)
    && typeof instance.updates.state === 'string'
    && instance.updates.state.length <= 40
    && nullableString(instance.updates.error, 360)
}

function positiveIntList(value, max = 64) {
  return Array.isArray(value)
    && value.length <= max
    && value.every(item => Number.isInteger(item) && item > 0)
}

// Optional plan-derived option ranges the account host may attach to the
// connection so the app can render the same plan-bounded resource pickers the
// mobius.you website shows. Absent on older hosts — treated as "unknown", the
// app then omits the pickers rather than guessing limits.
function validPlanLimits(value) {
  // Lenient: require the known fields with correct types, but tolerate empty
  // option lists and extra keys a newer account host may add. parseRailway DROPS
  // a plan_limits that fails this rather than rejecting the whole payload, so
  // shape drift only costs the pickers, never the deployments/connection panel.
  return value !== null
    && typeof value === 'object'
    && !Array.isArray(value)
    && positiveIntList(value.cpu_choices)
    && Number.isInteger(value.max_cpu) && value.max_cpu > 0
    && positiveIntList(value.memory_options_mb)
    && Number.isInteger(value.max_memory_mb) && value.max_memory_mb > 0
    && positiveIntList(value.volume_options_mb)
    && Number.isInteger(value.default_volume_mb) && value.default_volume_mb > 0
}

function validImageUpdatePolicies(value) {
  return Array.isArray(value)
    && value.length === IMAGE_UPDATE_POLICIES.length
    && new Set(value).size === value.length
    && value.every(policy => IMAGE_UPDATE_POLICIES.includes(policy))
}

function validRailwayRegions(value) {
  return Array.isArray(value)
    && value.length === RAILWAY_REGION_IDS.length
    && new Set(value.map(item => item?.id)).size === value.length
    && value.every(item => exactKeys(item, ['id', 'label'])
      && RAILWAY_REGION_IDS.includes(item.id)
      && typeof item.label === 'string'
      && item.label.length > 0
      && item.label.length <= 80)
}

export function parseRailway(value) {
  if (
    !exactKeys(value, ['railway_access', 'connection', 'instances'])
    || !RAILWAY_ACCESS.includes(value.railway_access)
    || !Array.isArray(value.instances)
    || value.instances.length > 100
    || !value.instances.every(validRailwayInstance)
  ) throw new Error('Möbius returned invalid Railway deployment state.')

  if (value.railway_access !== 'available') {
    if (value.connection !== null || value.instances.length) {
      throw new Error('Möbius exposed Railway details without account access.')
    }
    return value
  }
  if (value.connection !== null) {
    const connection = value.connection
    if (
      !exactKeys(connection, [
        'connected', 'account', 'workspace', 'plan', 'deploy_blocked',
      ], ['plan_limits', 'update_policies', 'adopt_current', 'region_options'])
      || typeof connection.connected !== 'boolean'
      || typeof connection.account !== 'string'
      || connection.account.length > 320
      || typeof connection.workspace !== 'string'
      || connection.workspace.length > 128
      || typeof connection.plan !== 'string'
      || connection.plan.length > 32
      || typeof connection.deploy_blocked !== 'string'
      || connection.deploy_blocked.length > 360
    ) throw new Error('Möbius returned an invalid Railway connection.')
    // Shape drift in the OPTIONAL plan_limits costs only the resource pickers:
    // drop it so a newer/mismatched host never blanks the whole Railway panel.
    if (connection.plan_limits !== undefined && !validPlanLimits(connection.plan_limits)) {
      delete connection.plan_limits
    }
    // Like plan limits, this is an advertised capability. A malformed extension
    // hides only the new controls; it never blanks the deployments panel.
    if (
      connection.update_policies !== undefined
      && !validImageUpdatePolicies(connection.update_policies)
    ) delete connection.update_policies
    if (
      connection.region_options !== undefined
      && !validRailwayRegions(connection.region_options)
    ) delete connection.region_options
    // Older account hosts advertised an importer for deployments created
    // outside Möbius. Retire that capability at this boundary so a staged
    // service rollout cannot revive its UI or blank the Railway panel.
    delete connection.adopt_current
  }
  return value
}

export function railwayAccountChanged(previousAccount, next) {
  return Boolean(
    next?.connection?.connected
    && next.connection.account
    && next.connection.account !== previousAccount
  )
}

export function parseDeletionDiagnosis(value) {
  const confirmable = value?.state === 'missing'
    || value?.state === 'missing_unconfirmed'
  if (
    !exactKeys(value, ['state', 'message', 'can_confirm_absent'])
    || !DELETION_STATES.includes(value.state)
    || typeof value.message !== 'string'
    || value.message.length < 1
    || value.message.length > 360
    || typeof value.can_confirm_absent !== 'boolean'
    || value.can_confirm_absent !== confirmable
  ) {
    throw new Error('Möbius returned invalid deletion recovery state.')
  }
  return value
}

export function deploymentNeedsTracking(instance) {
  return TRACKED_DEPLOYMENT_STATUSES.has(String(instance?.status || '').toLowerCase())
    || TRACKED_UPDATE_STATES.has(String(instance?.updates?.state || '').toLowerCase())
}

// Recovery repairs a running or failed deployment; the account service can
// still withhold it with an explicit `actions.recover: false`.
export function deploymentCanRecover(instance) {
  return instance?.actions?.recover !== false
    && ['ready', 'error'].includes(String(instance?.status || '').toLowerCase())
}

export function deploymentIsBuilding(instance) {
  return ['queued', 'creating', 'deploying'].includes(
    String(instance?.status || '').toLowerCase(),
  )
}

export function deploymentPresentation(instance) {
  const status = String(instance?.status || '').toLowerCase()
  const step = String(instance?.current_step || '').trim()
  const error = String(instance?.last_error || '').trim()

  if (status === 'ready' || status === 'active') {
    return {
      label: 'Active',
      detail: step && step.toLowerCase() !== 'ready' ? step : '',
      tone: 'success',
      actionLabel: 'Manage',
    }
  }
  if (status === 'deleting') {
    return {
      label: 'Deleting',
      detail: 'Railway is removing this project. This page will update automatically.',
      tone: 'progress',
      actionLabel: 'View',
    }
  }
  if (status === 'delete_failed') {
    const staleBuildCopy = /check the build/i.test(error)
    return {
      label: 'Deletion needs attention',
      detail: error && !staleBuildCopy ? error : DELETE_CONFIRMATION_FALLBACK,
      tone: 'danger',
      actionLabel: 'Review',
    }
  }
  if (deploymentIsBuilding(instance)) {
    return {
      label: 'Deploying',
      detail: step || 'Möbius is following the Railway build.',
      tone: 'progress',
      actionLabel: 'View',
    }
  }
  if (status === 'error') {
    return {
      label: 'Needs attention',
      detail: error || step || 'This deployment needs your attention.',
      tone: 'danger',
      actionLabel: 'Review',
    }
  }
  return {
    label: step || instance?.status || 'Status unavailable',
    detail: error,
    tone: 'muted',
    actionLabel: 'Manage',
  }
}

export function parseLinkAttempt(value) {
  const fields = ['authorization_url', 'attempt', 'state', 'expires_at']
  if (
    !exactKeys(value, fields)
    || !ACCOUNT_LINK_ATTEMPT.test(value.attempt)
    || !ACCOUNT_LINK_STATE.test(value.state)
    || typeof value.expires_at !== 'string'
    || !Number.isFinite(Date.parse(value.expires_at))
  ) {
    throw new Error('Möbius returned an invalid sign-in attempt.')
  }
  const authorization = webUrl(value.authorization_url)
  if (
    !authorization
    || authorization.hash
    || authorization.searchParams.getAll('state').length !== 1
    || authorization.searchParams.get('state') !== value.state
  ) {
    throw new Error('Möbius returned an invalid sign-in address.')
  }
  return {
    ...value,
    authorization_origin: authorization.origin,
  }
}

export function accountStatus(identity) {
  if (!identity) return { label: 'Account unavailable', tone: 'error' }
  if (identity.account_unavailable) {
    return {
      label: identity.account_mode === 'managed'
        ? 'Managed account unavailable'
        : 'Linked account unavailable',
      tone: 'warning',
    }
  }
  if (identity.account_mode === 'managed') {
    return { label: 'Managed by mobius.you', tone: 'online' }
  }
  if (identity.account_mode === 'linked') {
    return { label: 'Linked to mobius.you', tone: 'online' }
  }
  return { label: 'Not signed in', tone: 'muted' }
}

export function waitForAccountLink({
  popup,
  attempt,
  signal,
  eventTarget = window,
  parentWindow = window.parent,
  shellOrigin = window.location.origin,
  now = Date.now,
  registrationTimeoutMs = BROKER_ACK_WINDOW_MS,
  closedPollMs = 350,
}) {
  const authorizationOrigin = attempt.authorization_origin
    || new URL(attempt.authorization_url).origin
  // The account service enforces the absolute expiry. Use a bounded local
  // window here so a skewed browser clock can neither reject a fresh attempt
  // immediately nor retain a broker registration indefinitely.
  const deadline = now() + ACCOUNT_LINK_WINDOW_MS

  return new Promise((resolve, reject) => {
    let settled = false
    let registered = false
    let closedTimer = null
    let expiryTimer = null
    let registrationTimer = null

    const unregister = () => {
      try {
        parentWindow.postMessage({
          type: 'moebius:account-link-unregister',
          state: attempt.state,
        }, shellOrigin)
      } catch { /* parent retired with this frame */ }
    }

    const closePopup = () => {
      try { popup?.close?.() } catch { /* cross-origin popup already gone */ }
    }

    const finish = (error, result) => {
      if (settled) return
      settled = true
      eventTarget.removeEventListener('message', receive)
      signal?.removeEventListener('abort', abort)
      clearInterval(closedTimer)
      clearTimeout(expiryTimer)
      clearTimeout(registrationTimer)
      unregister()
      closePopup()
      error ? reject(error) : resolve(result)
    }

    const receive = event => {
      if (event.source !== parentWindow || event.origin !== shellOrigin) return
      const message = event.data
      if (
        !registered
        && exactKeys(message, ['type', 'state'])
        && message.type === 'moebius:account-link-registered'
        && message.state === attempt.state
      ) {
        registered = true
        clearTimeout(registrationTimer)
        try {
          popup.location.replace(attempt.authorization_url)
        } catch {
          finish(new Error('The sign-in window could not be opened. Please try again.'))
        }
        return
      }
      if (
        !registered
        || !exactKeys(message, ['type', 'code', 'state', 'authorizationOrigin'])
        || message.type !== 'moebius:account-link-result'
        || message.authorizationOrigin !== authorizationOrigin
        || message.state !== attempt.state
        || typeof message.code !== 'string'
        || !ACCOUNT_LINK_CODE.test(message.code)
      ) return
      finish(null, { code: message.code, state: message.state })
    }

    const abort = () => finish(new Error('Sign-in cancelled.'))

    eventTarget.addEventListener('message', receive)
    signal?.addEventListener('abort', abort, { once: true })
    closedTimer = setInterval(() => {
      if (popup.closed) {
        finish(new Error('The sign-in window was closed. Try again when you are ready.'))
      }
    }, closedPollMs)
    expiryTimer = setTimeout(() => {
      finish(new Error('Sign-in took too long. Please try again.'))
    }, Math.max(0, deadline - now()))
    registrationTimer = setTimeout(() => {
      finish(new Error('Möbius could not prepare secure sign-in. Please try again.'))
    }, registrationTimeoutMs)

    if (signal?.aborted) {
      abort()
      return
    }
    try {
      parentWindow.postMessage({
        type: 'moebius:account-link-register',
        authorizationOrigin,
        state: attempt.state,
        expiresAt: attempt.expires_at,
      }, shellOrigin)
    } catch {
      finish(new Error('Möbius could not prepare secure sign-in. Please try again.'))
    }
  })
}
