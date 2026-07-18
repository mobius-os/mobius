// CANONICAL DIFF VIEWER: copy this entire folder verbatim. It imports only
// React and its own flat sibling modules. Styles ship as a JavaScript string
// because the mini-app compiler rejects CSS side-output.

const DIFF_HEADER_PREFIX = 'diff --git '
const HUNK_HEADER = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/

const ESCAPES = {
  a: '\x07',
  b: '\b',
  f: '\f',
  n: '\n',
  r: '\r',
  t: '\t',
  v: '\v',
  '\\': '\\',
  '"': '"',
}

// Git quotes unusual paths with C-style escapes. Decode the common escapes and
// octal UTF-8 bytes so parsed paths continue to match the API's decoded paths.
export function decodeGitPath(value) {
  const trimmed = typeof value === 'string' ? value.trim() : ''
  if (!(trimmed.startsWith('"') && trimmed.endsWith('"'))) return trimmed

  const input = trimmed.slice(1, -1)
  let output = ''
  for (let index = 0; index < input.length;) {
    if (input[index] !== '\\') {
      output += input[index]
      index += 1
      continue
    }

    const octets = []
    while (input[index] === '\\' && /^[0-7]{3}/.test(input.slice(index + 1, index + 4))) {
      octets.push(Number.parseInt(input.slice(index + 1, index + 4), 8))
      index += 4
    }
    if (octets.length > 0) {
      output += new TextDecoder().decode(Uint8Array.from(octets))
      continue
    }

    const escaped = input[index + 1]
    output += ESCAPES[escaped] ?? escaped ?? '\\'
    index += escaped === undefined ? 1 : 2
  }
  return output
}

function withoutSidePrefix(value, side) {
  const path = decodeGitPath(value)
  if (!path || path === '/dev/null') return null
  const prefix = `${side}/`
  return path.startsWith(prefix) ? path.slice(prefix.length) : path
}

function quotedToken(input, start = 0) {
  if (input[start] !== '"') return null
  let escaped = false
  for (let index = start + 1; index < input.length; index += 1) {
    if (!escaped && input[index] === '"') {
      return { token: input.slice(start, index + 1), end: index + 1 }
    }
    if (!escaped && input[index] === '\\') escaped = true
    else escaped = false
  }
  return { token: input.slice(start), end: input.length }
}

function parseDiffHeader(line) {
  const value = line.slice(DIFF_HEADER_PREFIX.length)
  if (value.startsWith('"')) {
    const oldToken = quotedToken(value)
    const newStart = value.slice(oldToken.end).search(/\S/)
    const offset = newStart < 0 ? value.length : oldToken.end + newStart
    const newToken = quotedToken(value, offset)
    return {
      oldPath: withoutSidePrefix(oldToken.token, 'a'),
      newPath: withoutSidePrefix(newToken?.token || value.slice(offset), 'b'),
    }
  }

  const separators = []
  let offset = value.indexOf(' b/')
  while (offset >= 0) {
    separators.push(offset)
    offset = value.indexOf(' b/', offset + 1)
  }
  if (separators.length === 0) {
    return { oldPath: withoutSidePrefix(value, 'a'), newPath: null }
  }

  // Unquoted paths may contain spaces. Prefer the split that yields identical
  // paths (the overwhelmingly common modify case), then the first valid split;
  // ---/+++ and rename/copy headers refine paths below when they differ.
  let split = separators[0]
  for (const candidate of separators) {
    const oldPath = withoutSidePrefix(value.slice(0, candidate), 'a')
    const newPath = withoutSidePrefix(value.slice(candidate + 1), 'b')
    if (oldPath === newPath) {
      split = candidate
      break
    }
  }
  return {
    oldPath: withoutSidePrefix(value.slice(0, split), 'a'),
    newPath: withoutSidePrefix(value.slice(split + 1), 'b'),
  }
}

function headerPath(line, prefix, side = null) {
  const raw = line.slice(prefix.length).replace(/\t.*$/, '')
  return side ? withoutSidePrefix(raw, side) : decodeGitPath(raw)
}

function finishEntry(entry) {
  let status = 'M'
  if (entry.renameFrom !== null || entry.renameTo !== null) status = 'R'
  else if (entry.copyFrom !== null || entry.copyTo !== null) status = 'C'
  else if (entry.newFile) status = 'A'
  else if (entry.deletedFile) status = 'D'
  else if (entry.oldMode && entry.newMode) status = 'T'

  if (entry.renameFrom !== null) entry.oldPath = entry.renameFrom
  if (entry.renameTo !== null) entry.newPath = entry.renameTo
  if (entry.copyFrom !== null) entry.oldPath = entry.copyFrom
  if (entry.copyTo !== null) entry.newPath = entry.copyTo

  const path = status === 'D'
    ? (entry.oldPath || entry.newPath || '')
    : (entry.newPath || entry.oldPath || '')
  // insertions/deletions mirror the app-update review's parseUpdateDiff summary
  // counts (0 for a binary file), so the app surface can adopt this parser
  // wholesale when the two review UIs converge into shared library code.
  let insertions = 0
  let deletions = 0
  for (const hunk of entry.hunks) {
    for (const diffLine of hunk.lines) {
      if (diffLine.type === 'add') insertions += 1
      else if (diffLine.type === 'del') deletions += 1
    }
  }
  return {
    path,
    oldPath: entry.oldPath,
    newPath: entry.newPath,
    status,
    binary: entry.binary,
    insertions,
    deletions,
    hunks: entry.binary ? [] : entry.hunks,
  }
}

function newEntry(paths) {
  return {
    ...paths,
    newFile: false,
    deletedFile: false,
    oldMode: false,
    newMode: false,
    renameFrom: null,
    renameTo: null,
    copyFrom: null,
    copyTo: null,
    binary: false,
    hunks: [],
    currentHunk: null,
    oldNo: null,
    newNo: null,
  }
}

/** Parse a git unified diff into independent file entries. */
export function parseUnifiedDiff(diffText) {
  if (typeof diffText !== 'string' || diffText.length === 0) return []

  const parsed = []
  let entry = null
  for (const line of diffText.split(/\r?\n/)) {
    if (line.startsWith(DIFF_HEADER_PREFIX)) {
      if (entry) parsed.push(finishEntry(entry))
      entry = newEntry(parseDiffHeader(line))
      continue
    }
    if (!entry) continue

    // File-header metadata only appears BEFORE a file's first hunk. Gating on
    // !currentHunk stops a diff-body line whose content starts with "-- " or
    // "++ " (raw line "--- …" / "+++ …", e.g. a removed SQL comment) from
    // being misread as a ---/+++ path header and clobbering the file's path.
    if (!entry.currentHunk) {
      if (line.startsWith('new file mode ')) entry.newFile = true
      else if (line.startsWith('deleted file mode ')) entry.deletedFile = true
      else if (line.startsWith('old mode ')) entry.oldMode = true
      else if (line.startsWith('new mode ')) entry.newMode = true
      else if (line.startsWith('rename from ')) entry.renameFrom = headerPath(line, 'rename from ')
      else if (line.startsWith('rename to ')) entry.renameTo = headerPath(line, 'rename to ')
      else if (line.startsWith('copy from ')) entry.copyFrom = headerPath(line, 'copy from ')
      else if (line.startsWith('copy to ')) entry.copyTo = headerPath(line, 'copy to ')
      else if (line.startsWith('--- ')) entry.oldPath = headerPath(line, '--- ', 'a')
      else if (line.startsWith('+++ ')) entry.newPath = headerPath(line, '+++ ', 'b')
      else if (/^Binary files .+ differ$/.test(line)) entry.binary = true
    }

    const hunkMatch = line.match(HUNK_HEADER)
    if (hunkMatch) {
      entry.currentHunk = { header: line, lines: [] }
      entry.hunks.push(entry.currentHunk)
      entry.oldNo = Number.parseInt(hunkMatch[1], 10)
      entry.newNo = Number.parseInt(hunkMatch[3], 10)
      continue
    }
    if (!entry.currentHunk || line === '') continue

    if (line.startsWith('\\ No newline at end of file')) {
      entry.currentHunk.lines.push({
        type: 'meta', text: line, oldNo: null, newNo: null,
      })
    } else if (line[0] === '+') {
      entry.currentHunk.lines.push({
        type: 'add', text: line.slice(1), oldNo: null, newNo: entry.newNo,
      })
      entry.newNo += 1
    } else if (line[0] === '-') {
      entry.currentHunk.lines.push({
        type: 'del', text: line.slice(1), oldNo: entry.oldNo, newNo: null,
      })
      entry.oldNo += 1
    } else if (line[0] === ' ') {
      entry.currentHunk.lines.push({
        type: 'context', text: line.slice(1), oldNo: entry.oldNo, newNo: entry.newNo,
      })
      entry.oldNo += 1
      entry.newNo += 1
    } else {
      // A cut-off final section can end on incomplete metadata. Preserve it as
      // a non-numbered line instead of rejecting the entire file entry.
      entry.currentHunk.lines.push({
        type: 'meta', text: line, oldNo: null, newNo: null,
      })
    }
  }

  if (entry) parsed.push(finishEntry(entry))
  return parsed
}
