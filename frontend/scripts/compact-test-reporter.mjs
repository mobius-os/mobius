// Compact node:test reporter for agent and CI output.
//
// Passing tests and test console output print nothing. Each failure prints,
// as soon as it happens, its name, file:line, error message, a bounded
// actual/expected excerpt, and the non-internal stack frames; a test file
// that crashes prints the tail of its own output. The run ends with one
// summary line naming a full spec-format log, which keeps every passing test,
// console output, and untruncated assertion values for follow-up reading.
// CI uses the same reporter: the failure blocks are the part a reviewer
// needs, and they stay greppable for scripts/ci-failures.sh. The built-in dot
// reporter is not enough: it reports a test file that crashes on load as only
// 'test failed', and it prints assertion values unbounded (a source-text
// assert.match dumps the whole file).
//
// Log path: $MOBIUS_TEST_LOG when set, otherwise one file per checkout and
// npm script under $TMPDIR/mobius-test-logs, overwritten by the next run.

import { createHash } from 'node:crypto'
import { createWriteStream, mkdirSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { finished } from 'node:stream/promises'
import { spec as SpecReporter } from 'node:test/reporters'
import { inspect } from 'node:util'

const MESSAGE_CHARS = 800
const VALUE_CHARS = 300
const STACK_FRAMES = 6
const OUTPUT_TAIL_LINES = 40
const OUTPUT_BUFFER_CHARS = 64 * 1024
const MAX_LISTED_CANCELLED = 5

function logPath() {
  if (process.env.MOBIUS_TEST_LOG) return path.resolve(process.env.MOBIUS_TEST_LOG)
  const label = (process.env.npm_lifecycle_event || 'node-test').replace(/[^A-Za-z0-9._-]+/g, '-')
  const checkout = createHash('sha256').update(process.cwd()).digest('hex').slice(0, 10)
  return path.join(os.tmpdir(), 'mobius-test-logs', `${label}-${checkout}.log`)
}

function clip(text, limit) {
  const value = String(text)
  if (value.length <= limit) return value
  return `${value.slice(0, limit)} ...[${value.length - limit} more chars in full log]`
}

function indent(text, prefix = '    ') {
  return String(text).split('\n').map(line => prefix + line).join('\n')
}

function relativeFile(file) {
  if (!file) return '<unknown file>'
  const bare = file.startsWith('file://') ? new URL(file).pathname : file
  const rel = path.relative(process.cwd(), bare)
  return rel && !rel.startsWith('..') ? rel : bare
}

function showValue(value) {
  return clip(inspect(value, { depth: 4, maxArrayLength: 20, maxStringLength: VALUE_CHARS, breakLength: 120 }), VALUE_CHARS)
}

function stackFrames(error) {
  if (typeof error?.stack !== 'string') return []
  return error.stack.split('\n')
    .filter(line => /^\s+at /.test(line))
    .filter(line => !/\(node:|\bat node:|node:internal/.test(line))
    .slice(0, STACK_FRAMES)
    .map(line => line.trim())
}

function describeError(error, depth = 0) {
  if (error == null) return ['(no error details)']
  if (typeof error !== 'object') return [clip(String(error), MESSAGE_CHARS)]
  const lines = []
  const name = error.name || error.constructor?.name || 'Error'
  const code = error.code && error.code !== 'ERR_ASSERTION' ? ` [${error.code}]` : ''
  lines.push(`${name}${code}: ${clip(error.message ?? '', MESSAGE_CHARS)}`)
  if ('actual' in error || 'expected' in error) {
    // assert.match/ok messages already quote the actual input; the values are
    // still useful for deepEqual-style failures where the message is generic.
    if (!error.generatedMessage || !['match', 'doesNotMatch', '==', 'fail'].includes(error.operator)) {
      lines.push(`actual:   ${showValue(error.actual)}`)
      lines.push(`expected: ${showValue(error.expected)}`)
    }
    if (error.operator) lines.push(`operator: ${error.operator}`)
  }
  for (const frame of stackFrames(error)) lines.push(frame)
  if (error.cause && depth < 2) {
    lines.push('cause:')
    lines.push(...describeError(error.cause, depth + 1).map(line => `  ${line}`))
  }
  return lines
}

function formatFailure(failure, outputByFile) {
  const out = []
  const error = failure.details?.error
  const cause = error?.cause ?? error
  out.push(`✖ ${failure.name}`)
  out.push(`  at ${relativeFile(failure.file)}:${failure.line ?? '?'}`)
  // A file-level failure with the generic "test failed" message is a test
  // process that crashed or never loaded; its own output carries the error
  // and has already arrived by the time the file-level result is reported.
  const fileLevelCrash = failure.nesting === 0
    && String(cause?.message ?? cause) === 'test failed'
    && failure.file && !failure.entryFile
  if (fileLevelCrash) {
    const output = (outputByFile.get(failure.file) || '').trimEnd().split('\n')
    out.push(`  test file exited unsuccessfully; last ${Math.min(output.length, OUTPUT_TAIL_LINES)} output lines:`)
    out.push(indent(output.slice(-OUTPUT_TAIL_LINES).join('\n')))
  } else {
    if (error?.failureType && error.failureType !== 'testCodeFailure') out.push(`  (${error.failureType})`)
    out.push(indent(describeError(cause).join('\n')))
  }
  return `${out.join('\n')}\n\n`
}

export default async function* compactReporter(source) {
  const fullLogPath = logPath()
  mkdirSync(path.dirname(fullLogPath), { recursive: true })
  const fullLog = createWriteStream(fullLogPath)
  const spec = new SpecReporter()
  spec.pipe(fullLog)

  let failureCount = 0
  const cancelled = []
  const errorDiagnostics = []
  const outputByFile = new Map()
  let summary = null
  let coverage = null

  const remember = (file, message) => {
    const key = file || '<runner>'
    const next = (outputByFile.get(key) || '') + message
    outputByFile.set(key, next.length > OUTPUT_BUFFER_CHARS ? next.slice(-OUTPUT_BUFFER_CHARS) : next)
  }

  for await (const event of source) {
    spec.write(event)
    const { type, data } = event
    if (type === 'test:stdout' || type === 'test:stderr') {
      remember(data.entryFile || data.file, data.message)
    } else if (type === 'test:fail') {
      // A failing todo test does not fail the run; the full log keeps it.
      if (data.todo) continue
      const error = data.details?.error
      const failureType = error?.failureType
      if (failureType === 'subtestsFailed') continue
      if (failureType === 'cancelledByParent') {
        cancelled.push(data)
        continue
      }
      failureCount += 1
      yield formatFailure(data, outputByFile)
    } else if (type === 'test:diagnostic' && data.level === 'error') {
      errorDiagnostics.push(data.message)
    } else if (type === 'test:summary' && !data.file) {
      summary = data
    } else if (type === 'test:coverage') {
      coverage = data.summary?.totals
    }
  }

  spec.end()
  await finished(fullLog).catch(() => {})

  const out = []
  if (cancelled.length > 0) {
    const listed = cancelled.slice(0, MAX_LISTED_CANCELLED).map(item => `${item.name} (${relativeFile(item.file)})`)
    const more = cancelled.length > listed.length ? `, +${cancelled.length - listed.length} more` : ''
    out.push(`cancelled by a failing parent: ${listed.join('; ')}${more}`)
  }
  for (const message of errorDiagnostics) out.push(message)
  if (coverage) {
    const pct = value => (typeof value === 'number' ? `${value.toFixed(2)}%` : '?')
    out.push(`coverage: lines ${pct(coverage.coveredLinePercent)}, branches ${pct(coverage.coveredBranchPercent)}, functions ${pct(coverage.coveredFunctionPercent)}`)
  }

  const label = process.env.npm_lifecycle_event || 'node --test'
  const counts = summary?.counts || {}
  const seconds = typeof summary?.duration_ms === 'number' ? `${(summary.duration_ms / 1000).toFixed(1)}s` : '?s'
  // A coverage threshold breach fails the run through an error diagnostic even
  // when every test passed, so it must not read as ok.
  const ok = (summary ? summary.success : failureCount === 0) && errorDiagnostics.length === 0
  const extra = ['cancelled', 'skipped', 'todo']
    .filter(key => counts[key])
    .map(key => `${counts[key]} ${key}`)
  const tally = `${counts.passed ?? '?'}/${counts.tests ?? '?'} passed`
    + (counts.failed ? `, ${counts.failed} failed` : '')
    + (extra.length ? `, ${extra.join(', ')}` : '')
  out.push(`${label}: ${ok ? 'ok' : 'FAILED'} - ${tally} in ${seconds}; full log: ${fullLogPath}`)
  yield `${out.join('\n')}\n`
}
