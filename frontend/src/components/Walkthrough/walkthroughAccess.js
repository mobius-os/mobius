/* The App Store's access disclosure, ported so the guide can show exactly what
   an app asks for before it is installed. Source of truth:
   /data/apps/store/ui/CapabilityContract.jsx (capabilityRows). Keep the two in
   step: a data grant must never be hidden from review because this copy lacks
   its polished wording (the unknown-grant fallback below covers new grants). */
import { apiFetch } from '../../api/client.js'
import { detailToMessage } from '../../lib/errorDetail.js'

function row(label, tag, summary, tone = '') {
  return { label, tag, summary, tone }
}

function accessText(level, subject) {
  if (level === 'write') return `Can read and write ${subject}.`
  return `Can read ${subject}, but cannot change it.`
}

// Boolean data capabilities belong in one disclosure registry. Keeping their
// owner-facing copy next to the contract key makes a newly introduced grant
// difficult to wire into the backend while accidentally hiding it in review.
const BOOLEAN_DATA_CAPABILITIES = [
  {
    key: 'filesystem_api',
    label: 'Owner files',
    tag: 'Access',
    summary: 'Can use the guarded owner-filesystem API.',
  },
  {
    key: 'github_access',
    label: 'GitHub data',
    tag: 'Access',
    summary: 'Can use the connected GitHub account.',
  },
  {
    key: 'github_connect',
    label: 'GitHub connection',
    tag: 'Manages',
    summary:
      'Can start, resume, refresh, cancel, or disconnect the owner’s GitHub connection.',
  },
  {
    key: 'manage_apps',
    label: 'Installed apps',
    tag: 'Manages',
    summary: 'Can install and uninstall apps.',
  },
  {
    key: 'manage_skills',
    label: 'Agent skills',
    tag: 'Manages',
    summary: 'Can install and remove agent skills.',
  },
]
const DISCLOSED_DATA_KEYS = new Set([
  'chat_logs',
  'shared_memory',
  'cross_app_access',
  'share_with_apps',
  ...BOOLEAN_DATA_CAPABILITIES.map(capability => capability.key),
])

function readableCapabilityKey(key) {
  return String(key || '')
    .split('_')
    .filter(Boolean)
    .map(word => word[0]?.toUpperCase() + word.slice(1))
    .join(' ')
}

function isActiveUnknownGrant(value) {
  return value !== false
    && value !== null
    && value !== undefined
    && value !== ''
    && value !== 'none'
}

export function capabilityRows(contract) {
  if (!contract || typeof contract !== 'object') return []
  const agent = contract.agent || {}
  const data = contract.data || {}
  const background = contract.background
  const runtime = contract.runtime || {}
  const prompt = agent.system_prompt
  const logs = data.chat_logs || {}
  const rows = []
  if (prompt) rows.push(row(
    'Agent chats', 'All chats',
    `Adds system instructions to every agent chat on its next turn (${prompt.file}).`,
    'write',
  ))
  if (logs.effective === 'summary') rows.push(row(
    'Chat history', 'Redacted',
    'Can request structurally redacted chat text; tool calls, hidden reasoning, errors, and secret-like values are removed.',
    'read',
  ))
  if (data.shared_memory === 'read' || data.shared_memory === 'write') rows.push(row(
    'Shared memory', data.shared_memory === 'write' ? 'Read + write' : 'Read',
    accessText(data.shared_memory, 'the shared memory area'),
    data.shared_memory,
  ))
  if (data.cross_app_access === 'read' || data.cross_app_access === 'write') rows.push(row(
    'Other apps’ data', data.cross_app_access === 'write' ? 'Read + write' : 'Read',
    accessText(data.cross_app_access, 'other apps’ private data'),
    data.cross_app_access,
  ))
  if (data.share_with_apps === 'read' || data.share_with_apps === 'write') rows.push(row(
    'Shares its data', data.share_with_apps === 'write' ? 'Read + write' : 'Read',
    data.share_with_apps === 'write'
      ? 'Allows authorized apps to read and change this app’s private data.'
      : 'Allows authorized apps to read this app’s private data.',
    data.share_with_apps,
  ))
  if (background) {
    const timing = background.mode === 'scheduled'
      ? `Runs on schedule ${background.cron || ''}`.trim()
      : 'Runs only on demand'
    const init = background.initialize_on_install
      ? ' and starts an initialization run immediately after install'
      : ''
    rows.push(row(
      'Background work', 'Server job',
      `${timing}${init}. It runs as reviewed owner-installed code with Möbius process access and receives a short-lived app token for API calls.`,
      'write',
    ))
  }
  if (Array.isArray(agent.skills) && agent.skills.length) {
    rows.push(row(
      'Agent skills', String(agent.skills.length),
      `Installs ${agent.skills.join(', ')} while the app is active.`,
      'read',
    ))
  }
  for (const capability of BOOLEAN_DATA_CAPABILITIES) {
    if (data[capability.key]) {
      rows.push(row(
        capability.label,
        capability.tag,
        capability.summary,
        'write',
      ))
    }
  }
  // Platform and App Store can ship independently. Never hide a newly added
  // data grant just because this Store version lacks its polished copy.
  for (const [key, value] of Object.entries(data).sort()) {
    if (!DISCLOSED_DATA_KEYS.has(key) && isActiveUnknownGrant(value)) {
      rows.push(row(
        readableCapabilityKey(key) || 'Additional data grant',
        'Review',
        `The platform reports the unrecognized “${key}” data grant. Update App Store for a detailed explanation.`,
        'write',
      ))
    }
  }
  for (const [capability, declaration] of Object.entries(runtime).sort()) {
    const limits = declaration?.limits || {}
    const duration = Number(limits.max_duration_ms)
    const durationText = Number.isFinite(duration)
      ? ` for up to ${Math.round(duration / 100) / 10} seconds per request`
      : ''
    rows.push(row(
      declaration?.title || capability,
      `v${declaration?.version || '?'}`,
      `${declaration?.description || capability}${durationText}${
        declaration?.reason ? ` Reason: ${declaration.reason}` : ''
      }`,
      declaration?.risk === 'device' ? 'write' : 'read',
    ))
  }
  return rows
}

const DEFAULT_ERROR = 'This app’s access could not be checked right now.'

/* Read-only: fetches the app's manifest and returns its capability contract
   and digest. Nothing is installed. */
export async function previewAppAccess(manifestUrl, signal) {
  const response = await apiFetch('/apps/preview', { method: 'POST', body: JSON.stringify({ manifest_url: manifestUrl }), signal, timeoutMs: 20_000 })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(detailToMessage(data.detail, DEFAULT_ERROR))
  return data
}

/* Installs exactly what the reviewer saw: the server refuses if the app's
   capabilities changed after `reviewedDigest` was computed. Resolves with
   { status: 'installed' } or { status: 'changed', preview } when the owner
   must review the new access first. */
export async function installReviewedApp(manifestUrl, reviewedDigest) {
  const response = await apiFetch('/apps/install', { method: 'POST', body: JSON.stringify({ manifest_url: manifestUrl, reviewed_capability_digest: reviewedDigest }), timeoutMs: 120_000 })
  const data = await response.json().catch(() => ({}))
  if (response.status === 409 && data.detail?.code === 'capability_changed') {
    return { status: 'changed', preview: { capability_contract: data.detail.capability_contract, capability_digest: data.detail.capability_digest } }
  }
  if (!response.ok) throw new Error(detailToMessage(data.detail, 'The app could not be installed.'))
  return { status: 'installed' }
}
