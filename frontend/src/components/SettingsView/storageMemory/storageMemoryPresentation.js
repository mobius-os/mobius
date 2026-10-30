/* Words, sizes and statuses for the Settings Storage and Memory box. */
import { formatRelativeTime } from '../../../lib/relativeTime.js'

const GB = 1e9
const MB = 1e6

// Decimal units, the same as the storage alert and the sizes hosts quote.
export function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return ''
  if (bytes >= GB) return `${(bytes / GB).toFixed(2)} GB`
  if (bytes >= MB || bytes === 0) return `${Math.round(bytes / MB)} MB`
  return `${Math.max(1, Math.round(bytes / 1e3))} KB`
}

/** A stored size that may be unmeasured or only partly measured. */
export function storedSize({ bytes, incomplete }) {
  if (bytes == null) return { value: 'Not measured', note: 'yet' }
  if (incomplete) return { value: `${formatBytes(bytes)}+`, note: 'partly measured' }
  return { value: formatBytes(bytes), note: 'stored' }
}

export function percentOf(part, whole) {
  if (!Number.isFinite(part) || !Number.isFinite(whole) || whole <= 0) return 0
  return Math.min(100, Math.max(0, (part / whole) * 100))
}

const STATUS = {
  paused: { label: 'Agents paused', tone: 'danger' },
  storageLow: { label: 'Low on storage', tone: 'warning' },
  memoryLow: { label: 'Memory is high', tone: 'warning' },
  storageGettingLow: { label: 'Storage getting low', tone: 'warning' },
}

/** The one status the box shows, or null when nothing needs the owner. */
export function boxStatus(overview) {
  const storage = overview?.storage?.level
  const memory = overview?.memory?.level
  if (storage === 'paused' || memory === 'paused') return STATUS.paused
  if (storage === 'low') return STATUS.storageLow
  if (memory === 'low') return STATUS.memoryLow
  if (storage === 'getting_low') return STATUS.storageGettingLow
  return null
}

export function storageMeter(storage) {
  const used = storage.total_bytes - storage.free_bytes
  return {
    label: 'Storage',
    value: `${formatBytes(used)} / ${formatBytes(storage.total_bytes)} used`,
    support: `${formatBytes(storage.free_bytes)} free`,
    percent: percentOf(used, storage.total_bytes),
    markPercent: percentOf(storage.total_bytes - storage.pause_below_bytes, storage.total_bytes),
    note: storage.level === 'paused'
      ? `Agents are paused. They start again above ${formatBytes(storage.pause_below_bytes)} free.`
      : `Agents pause below ${formatBytes(storage.pause_below_bytes)} free.`,
    tone: storage.level,
  }
}

export function memoryMeter(memory) {
  return {
    label: 'Memory',
    value: `${formatBytes(memory.used_bytes)} / ${formatBytes(memory.limit_bytes)} used`,
    support: 'For running work and Möbius',
    percent: percentOf(memory.used_bytes, memory.limit_bytes),
    markPercent: percentOf(memory.pause_at_bytes, memory.limit_bytes),
    note: `Agents pause at ${formatBytes(memory.pause_at_bytes)}.`,
    tone: memory.level,
  }
}

export const CLEANUP_COPY = {
  work_files: {
    label: 'Chat work files',
    description: 'Temporary work from inactive chats.',
    deleted: 'Temporary files in chats that are not running.',
    kept: 'Files of running chats and of chats changed in the last 15 minutes. All messages, uploads and apps.',
    after: 'A chat that needs a file makes it again.',
    units: ['chat', 'chats'],
  },
  // These are the providers' own copies, not chats; a count here read as
  // "444 chats deleted", so this row states the size and that chats stay.
  agent_sessions: {
    label: 'Agent working copies',
    description: 'Includes recent copies; only ones older than 2 days clear.',
    context: 'Claude and Codex keep their own working copies for 14 days so an agent can continue where it stopped. This clears only copies older than 2 days.',
    deleted: "The agents' own copies older than 2 days.",
    kept: 'Every chat and message, copies from the last 2 days, copies of running chats, and your sign-ins.',
    after: 'When an old working copy is removed, the agent restarts from recent messages and may remember fewer details. Older helper conversations may no longer open.',
    reassurance: 'Your chats and messages stay.',
  },
  agent_browsers: {
    label: 'Agent browser data',
    description: 'Saved website data; sign-ins may need redoing.',
    deleted: 'Browser data of chats that are not running.',
    kept: 'Browsers of running chats and of chats used in the last 15 minutes.',
    after: 'Agents must sign in to websites again.',
    units: ['chat browser', 'chat browsers'],
  },
}

// One helper line under each details heading says what the group is for.
export const GROUP_COPY = {
  clear: { title: 'You can clear', helper: 'Free storage by removing agent files. Your chats, uploads, apps and app data stay.' },
  running: { title: 'Running now', helper: 'Stop the current turn, not the chat. Your messages stay.' },
  kept: { title: 'Kept to run Möbius', helper: 'Your chats, uploads, apps and Möbius itself. Clear never touches these.' },
}

/** A warning points to actions that relieve that resource, never auto-cleanup. */
export function resourceWarning(overview) {
  const storageLow = Boolean(overview?.storage?.level)
  const memoryLow = Boolean(overview?.memory?.level)
  if (!storageLow && !memoryLow) return null
  const paused = overview.storage?.level === 'paused' || overview.memory?.level === 'paused'
  const actions = []
  if (storageLow) actions.push({ group: 'clear', label: 'See cleanup options' })
  if (memoryLow) actions.push({ group: 'running', label: 'See running work' })
  const description = storageLow && memoryLow
    ? 'Storage is low and memory is high. Clear eligible files for storage, or stop running work for memory.'
    : storageLow
      ? 'Clear eligible agent files to free storage. Your chats and apps stay.'
      : 'Stop a running turn to reduce memory use. Clearing files frees storage instead.'
  return { title: paused ? 'Agents cannot start' : boxStatus(overview).label, description, actions,
    tone: paused ? 'danger' : 'warning' }
}

/** Helper chats are titled "Delegation · <task key>"; show a readable name instead of the key's id suffix. */
export function runningTurnTitle(title) {
  const match = /^Delegation · (.*)$/.exec(title || '')
  if (!match) return title || 'Untitled chat'
  const name = match[1].replace(/[-_:]?[0-9a-f]{12,}$/i, '').replace(/[-_]+/g, ' ').trim()
  return name ? `Helper agent: ${name}` : 'Helper agent'
}

export function memoryGroupLabel(label) {
  return { 'Agent turns': 'Agent processes', Other: 'Other processes' }[label] || label
}

const KEPT_COPY = {
  database: 'Your chats and messages, app records and settings.',
  chats: 'Files you uploaded and pictures shown in chats.',
  apps: 'Your apps and their saved data.',
  platform: 'Möbius itself and the tools it runs.',
  worktrees: 'Code copies agents use for longer projects.',
  contributions: 'Code copies Contribute uses for pull requests. Contribute removes finished ones.',
  shared: 'Files shared by apps, skills and Memory.',
  compiled: 'Ready-to-run builds of your apps.',
  logs: 'Möbius logs and its storage history.',
  app_secrets: 'Encrypted keys your apps use.',
  work: 'Agent work folders.',
}

export function keptDescription(id) {
  return KEPT_COPY[id] || ''
}

export function keptTotal(kept) {
  return (kept || []).reduce((sum, row) => sum + (row.bytes || 0), 0)
}

export function previewSentence(target, preview) {
  if (!preview?.bytes) return 'Nothing to clear right now.'
  const copy = CLEANUP_COPY[target]
  const size = `This frees about ${formatBytes(preview.bytes)}`
  if (!copy.units) return `${size}. ${copy.reassurance}`
  const [one, many] = copy.units
  const count = preview.items === 1 ? `1 ${one}` : `${preview.items} ${many}`
  return `${size} from ${count}.`
}

export function resultSentence(cleanup, storage) {
  const freed = cleanup.bytes ? `Cleared ${formatBytes(cleanup.bytes)}.` : 'Nothing was cleared.'
  const free = storage ? ` ${formatBytes(storage.free_bytes)} free now.` : ''
  return `${freed}${free}${cleanup.note ? ` ${cleanup.note}` : ''}`
}

/** The refresh row's freshness: folder sizes are the slowest reading to go stale. */
export function syncedLabel({ measuredAt, measuring = false, failed = false }, now = Date.now()) {
  if (measuring) return 'Syncing…'
  if (failed) return 'Sync failed'
  const relative = measuredAt ? formatRelativeTime(measuredAt, now) : ''
  if (!relative) return 'Not synced yet'
  return `Synced ${relative === 'now' ? 'just now' : relative}`
}
