// Envelope-aware formatting for a tool result string, so ToolBlock can render
// bash output as a terminal and structured results as clean key/values instead
// of dumping raw JSON into a <pre>.
//
// The input is always the string `tool.output` (streamReducers stores it as a
// string). This module is a PURE function — no React, no DOM — so the parsing
// rules are unit-testable in isolation (see toolResultFormat.test.js).
//
// Contract: formatToolResult(output) returns one of
//   { kind: 'text',       text }                          — plain / unparseable
//   { kind: 'terminal',   stdout, stderr, exitCode }      — shell output
//   { kind: 'structured', entries: [{ key, value }] }     — object result
// The caller (ToolBlock) branches on `kind`. `text` is the safe default: if the
// output isn't JSON, or is JSON we don't recognize as an envelope, it renders
// exactly as it does today. This function never throws.

// A shell tool result carries these keys. Presence of stdout OR stderr OR
// exit_code (any subset) is enough to treat it as a terminal envelope — some
// tools omit an empty stream.
const TERMINAL_KEYS = ['stdout', 'stderr', 'exit_code', 'exitCode']

// The Claude Code Bash tool reports a FAILURE as a plain-text result prefixed
// "Exit code N\n<stderr>" (a success is plain stdout with no prefix). This is
// the real failure signal for Claude bash — the JSON {stdout,stderr,exit_code}
// envelope is what Codex / MCP tools use instead. Verified against a live turn:
// a failed `cat` stores output "Exit code 1\ncat: ...: No such file...".
const EXIT_PREFIX = /^Exit code (\d+)\r?\n([\s\S]*)$/

// A "common" envelope wraps the real payload under one of these keys. We unwrap
// to the inner value and re-run the whole classification on it, so a result
// like {"result": "{\"stdout\": ...}"} still lands as a terminal.
const COMMON_KEYS = ['result', 'content', 'text', 'summary', 'output', 'data']

// Cap how many characters of any single rendered field we hand to the DOM. A
// multi-megabyte stdout blob in a <pre> janks scroll; the caller shows a
// "truncated" hint when this fires. Generous enough that normal output is whole.
const MAX_FIELD = 20000

function truncate(s) {
  if (typeof s !== 'string') return { text: s, truncated: false }
  if (s.length <= MAX_FIELD) return { text: s, truncated: false }
  return { text: s.slice(0, MAX_FIELD), truncated: true }
}

// Attempt to parse a string as JSON. Returns the parsed value, or undefined if
// it isn't JSON (so callers can distinguish `null`/`false` payloads from "not
// JSON"). Only strings that look like a JSON container are tried, so a plain
// sentence like "42 files changed" isn't reinterpreted as the number 42.
function tryParse(s) {
  if (typeof s !== 'string') return undefined
  const t = s.trim()
  if (!t) return undefined
  const first = t[0]
  if (first !== '{' && first !== '[') return undefined
  try {
    return JSON.parse(t)
  } catch {
    return undefined
  }
}

function hasAny(obj, keys) {
  return keys.some(k => Object.prototype.hasOwnProperty.call(obj, k))
}

function pickString(obj, key) {
  const v = obj[key]
  if (v == null) return ''
  return typeof v === 'string' ? v : JSON.stringify(v, null, 2)
}

// Render a single structured value into a display string. Objects/arrays are
// pretty-printed; scalars are stringified plainly (no wrapping quotes on
// strings, so a value reads as text not a JSON literal).
function displayValue(v) {
  if (v == null) return String(v)
  if (typeof v === 'string') return v
  if (typeof v === 'object') return JSON.stringify(v, null, 2)
  return String(v)
}

function classify(value, depth) {
  // Unwrap a common envelope, then re-classify the inner payload. Bounded to a
  // few levels so a pathological nesting can't spin (mirrors Hermex's 3-level
  // unescape).
  if (depth < 4 && value && typeof value === 'object' && !Array.isArray(value)) {
    const keys = Object.keys(value)
    // Terminal envelope ONLY when EVERY key is a terminal field. An object that
    // carries stdout/exit_code ALONGSIDE other payload (e.g. an MCP result with
    // `{stdout, files, count}`) is structured — treating it as terminal would
    // silently drop the sibling keys.
    if (hasAny(value, TERMINAL_KEYS) && keys.every(k => TERMINAL_KEYS.includes(k))) {
      const stdout = truncate(pickString(value, 'stdout'))
      const stderr = truncate(pickString(value, 'stderr'))
      const rawExit = value.exit_code ?? value.exitCode
      const exitCode = typeof rawExit === 'number' ? rawExit : null
      return {
        kind: 'terminal',
        stdout: stdout.text,
        stderr: stderr.text,
        exitCode,
        truncated: stdout.truncated || stderr.truncated,
      }
    }
    // Single-key common envelope: unwrap and recurse. Only when the object is
    // JUST the wrapper (one key) do we descend — an object with a `content`
    // field alongside real data is structured, not a wrapper.
    if (keys.length === 1 && COMMON_KEYS.includes(keys[0])) {
      const inner = value[keys[0]]
      // A wrapper around nothing is empty text, not the literal "null".
      if (inner == null) return { kind: 'text', text: '', truncated: false }
      const parsed = typeof inner === 'string' ? tryParse(inner) : inner
      if (parsed !== undefined) return classify(parsed, depth + 1)
      return classify(String(inner), depth + 1)
    }
    // A plain object → key/value rows, each value length-capped like the other
    // kinds so a huge structured field can't dump megabytes into the DOM.
    const entries = keys.map(k => {
      const t = truncate(displayValue(value[k]))
      return { key: k, value: t.text, truncated: t.truncated }
    })
    if (entries.length > 0) {
      return { kind: 'structured', entries, truncated: entries.some(e => e.truncated) }
    }
  }

  if (typeof value === 'string') {
    // The inner payload was itself a JSON string — re-parse once more.
    const parsed = tryParse(value)
    if (depth < 4 && parsed !== undefined) return classify(parsed, depth + 1)
    const t = truncate(value)
    return { kind: 'text', text: t.text, truncated: t.truncated }
  }

  // Arrays and scalars: render as pretty text rather than inventing rows.
  const t = truncate(displayValue(value))
  return { kind: 'text', text: t.text, truncated: t.truncated }
}

export function formatToolResult(output, { terminal = false } = {}) {
  if (output == null) return { kind: 'text', text: '', truncated: false }
  if (typeof output !== 'string') {
    // Defensive: a non-string slipped through. Pretty-print it.
    const t = truncate(displayValue(output))
    return { kind: 'text', text: t.text, truncated: t.truncated }
  }
  const parsed = tryParse(output)
  if (parsed === undefined) {
    // Not JSON. Detect the Claude Bash "Exit code N\n<stderr>" failure format
    // (nonzero only — a success carries no prefix) so a failed command shows
    // its exit code + stderr and marks the block/group failed.
    const m = output.match(EXIT_PREFIX)
    if (m && Number(m[1]) !== 0) {
      const msg = truncate(m[2])
      return {
        kind: 'terminal',
        stdout: '',
        stderr: msg.text,
        exitCode: Number(m[1]),
        truncated: msg.truncated,
      }
    }
    const t = truncate(output)
    // CommandExecution items usually stream plain stdout rather than a JSON
    // envelope. The caller knows the tool type, so its terminal hint lets that
    // successful output use the same stdout/stderr presentation as enveloped
    // results without guessing that every multiline string is a command.
    if (terminal) {
      return {
        kind: 'terminal',
        stdout: t.text,
        stderr: '',
        exitCode: null,
        truncated: t.truncated,
      }
    }
    return { kind: 'text', text: t.text, truncated: t.truncated }
  }
  const classified = classify(parsed, 0)
  if (!terminal || classified.kind === 'terminal') return classified

  // Valid JSON printed by a shell command is still stdout. Only a recognized
  // terminal envelope may reinterpret it; otherwise keep the command's exact
  // bytes instead of changing presentation based on what stdout happens to
  // contain (for example `curl ... | jq`).
  const t = truncate(output)
  return {
    kind: 'terminal',
    stdout: t.text,
    stderr: '',
    exitCode: null,
    truncated: t.truncated,
  }
}

// Did this tool result report a failure? True only when the output parses to a
// terminal envelope with a nonzero exit code. This is the ONLY failure signal a
// tool block carries — the stream contract never sets a tool `status` beyond
// 'running' → 'done' (see streamReducers.js), so both the per-block header
// indicator (ToolBlock) and the stretch's exit chip (ActivityStretch, via
// groupBlocks) derive "failed" from here rather than a status that never arrives.
export function toolResultFailed(output) {
  const r = formatToolResult(output)
  return r.kind === 'terminal' && r.exitCode != null && r.exitCode !== 0
}

// Did this tool BLOCK report a failure? Field-or-parse (contract rule 6): a
// block reduced at the funnel carries an explicit `output_exit_code`, so read
// that when present rather than re-parsing a possibly-carved excerpt (a JSON
// envelope truncated inside its stdout, or a bash head+tail). Absent the field
// (a small un-reduced output, or a live streaming item) fall back to parsing
// the output — the excerpt preserves the failure head, so the parse still
// works. Shared by ToolBlock's header chip and groupBlocks' Failed state.
export function toolBlockExitCode(t) {
  if (t && t.output_exit_code != null) return t.output_exit_code
  const r = formatToolResult(t?.output)
  return r.kind === 'terminal' ? r.exitCode : null
}

export function toolBlockFailed(t) {
  const code = toolBlockExitCode(t)
  return code != null && code !== 0
}
