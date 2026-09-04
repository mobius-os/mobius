/* Parse the bounded provider-neutral diff carried by edit tool blocks. */

import { parseUnifiedDiff } from '../DiffView/parseUnifiedDiff.js'

const HUNK_HEADER = /^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@/
const FILE_METADATA = /^(?:new file mode |deleted file mode |old mode |new mode |similarity index |dissimilarity index |rename from |copy from |index |--- |Binary files |GIT binary patch$)/

function isFileSectionStart(lines, index) {
  if (!lines[index]?.startsWith('diff --git ')) return false
  if (index === 0) return true
  return FILE_METADATA.test(lines[index + 1] || '')
}

function hunkRange(start, count) {
  if (count === 0) return `${start},0`
  return count === 1 ? String(start) : `${start},${count}`
}

function normalizeRawFileBody(section) {
  if (
    section.some(line => HUNK_HEADER.test(line))
    || section.some(line => /^(?:Binary files .+ differ|GIT binary patch)$/.test(line))
  ) return section

  const added = section.some(line => line.startsWith('new file mode '))
  const deleted = section.some(line => line.startsWith('deleted file mode '))
  if (!added && !deleted) return section

  const bodyStart = section.findIndex(line => line.startsWith('+++ ')) + 1
  if (bodyStart === 0) return section
  const body = section.slice(bodyStart)
  // split() retains one sentinel after a trailing newline. It is not another
  // source line, but earlier empty entries are real blank lines in the file.
  if (body.at(-1) === '') body.pop()
  if (body.length === 0) return section.slice(0, bodyStart)

  const oldCount = deleted ? body.length : 0
  const newCount = added ? body.length : 0
  const header = `@@ -${hunkRange(oldCount ? 1 : 0, oldCount)} +${
    hunkRange(newCount ? 1 : 0, newCount)
  } @@`
  const sign = added ? '+' : '-'
  return [
    ...section.slice(0, bodyStart),
    header,
    ...body.map(line => `${sign}${line}`),
  ]
}

/** Repair the pre-normalization Codex payload retained in existing chats.
 * New previews are normalized at ingestion, but settled transcript data is a
 * real user contract and must become readable without rewriting chat history.
 */
function normalizeLegacyCodexDiff(diff) {
  const lines = diff.split(/\r?\n/)
  const starts = lines.flatMap((_, index) => (
    isFileSectionStart(lines, index) ? [index] : []
  ))
  if (starts.length === 0) return diff
  starts.push(lines.length)
  const sections = starts.slice(0, -1).map((start, index) => (
    normalizeRawFileBody(lines.slice(start, starts[index + 1]))
  ))
  return sections.flat().join('\n')
}

export function toolEditPreview(value) {
  if (!value || typeof value !== 'object' || typeof value.diff !== 'string') {
    return null
  }
  const relative = value.relative === true
  const parsedFiles = parseUnifiedDiff(
    relative ? value.diff : normalizeLegacyCodexDiff(value.diff),
  )
  const files = relative
    ? parsedFiles.map(file => ({
        ...file,
        hunks: file.hunks.map((hunk, index) => ({
          ...hunk,
          header: `Changed selection${file.hunks.length > 1 ? ` ${index + 1}` : ''}`,
        })),
      }))
    : parsedFiles
  if (files.length === 0) return null
  return {
    files,
    relative,
    truncated: value.truncated === true,
  }
}
