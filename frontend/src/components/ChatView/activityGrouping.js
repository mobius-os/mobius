import { isDistinctiveActivityTool } from './toolActivityLabel.js'

// One boundary rule for transcript-level agent activity. Helper completions are
// incoming agent activity just like peer messages are outgoing agent activity;
// neither should split thinking/tools into repeated top-level disclosures.
export function isActivityRunEntry(entry) {
  const item = entry?.item
  return item?.type === 'thinking'
    || item?.type === 'helper_result'
    || (item?.type === 'tool' && !isDistinctiveActivityTool(item))
}

const MAX_OPERATION_RESOURCES = 128

function appOperationKey(item) {
  const activity = item?.type === 'tool' ? item.app_activity : null
  const key = typeof activity?.operation_key === 'string' ? activity.operation_key : ''
  const slug = typeof activity?.app_slug === 'string' ? activity.app_slug : ''
  return key && slug ? `${slug}\u0000${key}` : ''
}

function mergedResources(earlier, later) {
  const seen = new Set()
  const merged = []
  for (const resource of [...(earlier || []), ...(later || [])]) {
    const key = `${resource?.label}\u0000${resource?.intent || ''}`
    if (seen.has(key)) continue
    seen.add(key)
    merged.push(resource)
    if (merged.length >= MAX_OPERATION_RESOURCES) break
  }
  return merged
}

// An app pages one operation across several calls when its output must fit a
// provider's tool-output limit (e.g. Memory reading four notes in two calls).
// Receipts from the same app sharing an `operation_key` (see
// backend/app/agent_activity.py) are one operation, so they render as one row:
// it keeps the FIRST call's slot and key (no jump while later pages stream in)
// and shows the latest call's status and wording with every page's resources.
// Render-only (MsgContent): cold-transcript preparation must keep every stored
// block, or its prefix frames lose the later pages and shift later keys.
export function foldAppActivityOperations(entries) {
  const slotByKey = new Map()
  const folded = []
  for (const entry of entries) {
    const key = appOperationKey(entry?.item)
    const slot = key ? slotByKey.get(key) : undefined
    if (slot === undefined) {
      if (key) slotByKey.set(key, folded.length)
      folded.push(entry)
      continue
    }
    const first = folded[slot]
    const latest = entry.item.app_activity
    folded[slot] = {
      ...first,
      item: {
        ...first.item,
        status: entry.item.status,
        app_activity: {
          ...latest,
          resources: mergedResources(first.item.app_activity.resources, latest.resources),
        },
      },
    }
  }
  return folded
}

// Fold adjacent thinking/tool entries into the exact activity stretches shared
// by rendering and cold-transcript preparation. Distinctive tools stand alone;
// prose and other entries preserve their original interleave positions.
// Pure: entry objects are carried through unchanged.
export function groupActivityRuns(entries) {
  const nodes = []
  let run = []

  const flush = () => {
    if (run.length) nodes.push({ group: run })
    run = []
  }

  for (const entry of entries) {
    const type = entry?.item?.type
    // Some providers persist empty separator text blocks between reasoning and
    // tool events. They have no visible content, so treating them as prose
    // splits one coherent activity into repeated disclosure rows. Meaningful
    // prose remains a boundary; only a truly transparent separator disappears.
    const content = entry?.item?.content
    if (type === 'text' && typeof content === 'string' && !content.trim()) continue
    if (isDistinctiveActivityTool(entry?.item)) {
      flush()
      nodes.push({ group: [entry] })
    } else if (isActivityRunEntry(entry)) {
      run.push(entry)
    } else {
      flush()
      nodes.push({ single: entry })
    }
  }
  flush()
  return nodes
}
