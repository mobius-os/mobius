/* Slash-command registry, fuzzy matching, and menu key resolution for the composer. */

/**
 * The composer's "/" menu.
 *
 * DISPATCH AUTHORITY LIVES IN THE BACKEND. A message only reaches a real
 * command when `_is_cli_slash_command` (backend/app/chat.py) recognises it —
 * that function is what keeps the command at character 0 by appending Möbius's
 * hidden context blocks BELOW the message instead of prepending them, which is
 * the only arrangement the Claude CLI dispatches on. This registry is the
 * PRESENTATION half of that same fact.
 *
 * Listing a command here that the backend does not dispatch is the failure
 * worth guarding: the menu would offer it, the user would pick it, and it
 * would silently degrade into ordinary prose with no error anywhere. The two
 * lists are pinned together by a backend parity test
 * (test_slash_command_registry_parity) that reads THIS file, so adding a
 * command in one place and not the other fails the suite rather than shipping
 * a dead entry.
 *
 * `providers` records where a command actually does something. Both current
 * runners expose Möbius's durable goal controls, so `/goal` is available in
 * Claude, Codex, and Evolve chats. The picker
 * still shows commands that are unavailable in the current chat, with a clear
 * explanation, so typing "/" never looks broken merely because the provider
 * changed. Availability is enforced separately from matching so an
 * unavailable row can be discoverable without ever being accepted.
 */
export const SLASH_COMMANDS = [
  {
    name: 'goal',
    args: '<what to keep working toward>',
    summary: 'Keep pursuing a goal across turns',
    detail: 'Runs until the goal holds. Clear it from the Goal rail.',
    providers: ['claude', 'codex', 'mobius'],
  },
]

/**
 * The command fragment being typed, or null when the composer isn't picking one.
 *
 * Slash mode is narrow on purpose. It opens on a leading "/" and closes the
 * moment the command is committed — the first space means the user has moved
 * on to writing arguments, and a menu hovering over that is noise. It also
 * declines anything holding a second "/", so a path typed as the first word
 * ("/data/apps/x is broken") never summons a command menu; the backend's
 * dispatch check refuses those for the same reason.
 */
export function slashQueryFor(input) {
  const text = input ?? ''
  if (!text.startsWith('/')) return null
  const rest = text.slice(1)
  if (/[\s/]/.test(rest)) return null
  return rest
}

/**
 * Subsequence score for `query` against a command name, or null for no match.
 *
 * Plain subsequence matching alone ranks badly once there are several
 * commands: every candidate containing the letters "scattered anywhere" ties
 * with the one the user obviously meant. So matches score higher when they run
 * consecutively, when they land early, and when they start the name — "go"
 * should put "goal" above a hypothetical "diagnose".
 */
export function fuzzyScore(query, name) {
  const q = (query ?? '').toLowerCase()
  const n = (name ?? '').toLowerCase()
  if (!q) return 0
  let score = 0
  let from = 0
  let previous = -2
  let streak = 0
  for (const char of q) {
    const hit = n.indexOf(char, from)
    if (hit === -1) return null
    streak = hit === previous + 1 ? streak + 1 : 0
    score += 10 + streak * 5 - Math.min(hit - from, 6)
    if (hit === 0) score += 8
    previous = hit
    from = hit + 1
  }
  return score
}

/**
 * Commands offered for the current composer text, best match first.
 *
 * Returns [] when the composer isn't in slash mode, which is also the signal
 * to keep the menu closed — callers don't need a second predicate.
 */
export function matchSlashCommands(input) {
  const query = slashQueryFor(input)
  if (query === null) return []
  return SLASH_COMMANDS
    .map((command) => ({ command, score: fuzzyScore(query, command.name) }))
    .filter((entry) => entry.score !== null)
    .sort((a, b) => b.score - a.score || a.command.name.localeCompare(b.command.name))
    .map((entry) => entry.command)
}

/** Whether selecting this command would dispatch on the current provider. */
export function slashCommandIsAvailable(command, provider) {
  return !command.providers?.length
    || (typeof provider === 'string' && command.providers.includes(provider))
}

/** Short user-facing reason for a visible but unavailable command. */
export function slashCommandUnavailableReason(command, provider) {
  if (slashCommandIsAvailable(command, provider)) return ''
  if (typeof provider !== 'string') return 'Available after this chat finishes loading.'
  const providers = command.providers.map(
    (name) => name.charAt(0).toUpperCase() + name.slice(1),
  )
  return `Available in ${providers.join(' or ')} chats.`
}

/** Focus and dismissal are the two UI facts that may hide valid matches. */
export function visibleSlashCommands(commands, { focused, dismissed } = {}) {
  return focused && !dismissed ? commands : []
}

/**
 * What an open menu should do with a keydown, or null to leave the key alone.
 *
 * Kept pure and separate from the bar's other key handling because the menu
 * has to win Enter and ArrowUp/ArrowDown while it is open — the same keys that
 * otherwise send the message and walk sent-message history. Returning null for
 * every key the menu doesn't claim is what lets the caller fall through to
 * that existing behaviour untouched.
 */
export function resolveSlashMenuKey(event, { open, count } = {}) {
  if (!open || !count) return null
  if (event.metaKey || event.ctrlKey || event.altKey) return null
  if (event.key === 'ArrowDown') return 'next'
  if (event.key === 'ArrowUp') return 'previous'
  if (event.key === 'Escape') return 'dismiss'
  // Tab completes without sending — the conventional "fill it in" key, and the
  // only way to accept a command when it is the sole thing typed on a touch
  // keyboard that has no Enter-vs-send distinction.
  if (event.key === 'Tab' && !event.shiftKey) return 'accept'
  if (event.key === 'Enter' && !event.shiftKey) return 'accept'
  return null
}

/** Composer text after accepting a command: the name plus the space its arguments start after. */
export function applySlashCommand(command) {
  return `/${command.name} `
}
