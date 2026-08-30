/* @-mention of project files: token detection, fuzzy ranking, insertion. */

/**
 * The composer's "@" file mention for project chats.
 *
 * A mention is presentation-only sugar: accepting one inserts the file's
 * ordinary path (under the data directory the agent already works in) as plain
 * composer text. Nothing downstream parses a special reference format — the
 * agent receives exactly what the owner could have typed by hand, so the
 * backend, transcript, and every provider stay mention-agnostic.
 *
 * Token rules mirror slash mode's narrowness: mention mode opens on an "@" at
 * the start of the text or after whitespace, tracks the whitespace-free token
 * typed after it, and closes the moment the token ends (a space means the
 * owner has moved on). An "@" inside a word ("user@example.com") never opens
 * the menu.
 */

import { fuzzyScore } from './slashCommands.js'

/** The active mention token at the end of `input`, or null. */
export function mentionQueryFor(input) {
  const text = input ?? ''
  const match = /(^|\s)@([^\s@]*)$/.exec(text)
  if (!match) return null
  return { start: text.length - match[2].length - 1, query: match[2] }
}

/**
 * Project files ranked for a mention query, best first.
 *
 * The basename is what owners usually think in, so it outweighs a match that
 * only lands somewhere in the directory chain. An empty query simply offers
 * the first files of the listing — the menu opening on a bare "@" is what
 * makes the feature discoverable.
 */
export function matchMentionFiles(query, files, limit = 8) {
  const candidates = (files ?? []).filter((file) => file?.type !== 'directory')
  if (!query) return candidates.slice(0, limit)
  return candidates
    .map((file) => {
      const name = fuzzyScore(query, file.name)
      const path = fuzzyScore(query, file.path)
      if (name === null && path === null) return null
      return { file, score: Math.max((name ?? -1) * 2, path ?? -1) }
    })
    .filter(Boolean)
    .sort((a, b) => b.score - a.score || a.file.path.localeCompare(b.file.path))
    .slice(0, limit)
    .map((entry) => entry.file)
}

/**
 * The ordinary filesystem path the agent receives for a mentioned file.
 *
 * `rootPath` is the project's stored locator: logical ("projects/<id>") for
 * every current row, absolute only on rolling-upgrade compatibility rows —
 * the same two shapes the backend's `_project_root` resolves.
 */
export function mentionAgentPath(rootPath, filePath) {
  const root = String(rootPath ?? '').replace(/\/+$/, '')
  const file = String(filePath ?? '').replace(/^\/+/, '')
  if (!root) return file
  return root.startsWith('/') ? `${root}/${file}` : `/data/${root}/${file}`
}

/** Composer text after accepting a mention: token replaced by the path. */
export function applyFileMention(input, mention, agentPath) {
  const text = input ?? ''
  const path = String(agentPath ?? '')
  // Preserve path identity when an ordinary filename contains whitespace or
  // another separator that would make plain prose ambiguous to the agent.
  const inserted = /\s/.test(path) ? JSON.stringify(path) : path
  return `${text.slice(0, mention.start)}${inserted} `
}
