import assert from 'node:assert/strict'
import test from 'node:test'
import {
  formatToolResult,
  toolResultFailed,
  toolBlockFailed,
  toolBlockExitCode,
} from '../toolResultFormat.js'

test('plain text passes through unchanged', () => {
  const r = formatToolResult('hello world\nsecond line')
  assert.equal(r.kind, 'text')
  assert.equal(r.text, 'hello world\nsecond line')
})

test('a sentence that starts with a number is NOT parsed as JSON', () => {
  const r = formatToolResult('42 files changed')
  assert.equal(r.kind, 'text')
  assert.equal(r.text, '42 files changed')
})

test('malformed JSON falls back to text', () => {
  const r = formatToolResult('{not valid json')
  assert.equal(r.kind, 'text')
  assert.equal(r.text, '{not valid json')
})

test('terminal envelope unwraps stdout/stderr/exit_code', () => {
  const r = formatToolResult(
    JSON.stringify({ stdout: 'build ok', stderr: '', exit_code: 0 })
  )
  assert.equal(r.kind, 'terminal')
  assert.equal(r.stdout, 'build ok')
  assert.equal(r.stderr, '')
  assert.equal(r.exitCode, 0)
})

test('terminal envelope surfaces a nonzero exit code', () => {
  const r = formatToolResult(
    JSON.stringify({ stdout: '', stderr: 'command not found', exit_code: 127 })
  )
  assert.equal(r.kind, 'terminal')
  assert.equal(r.exitCode, 127)
  assert.equal(r.stderr, 'command not found')
})

test('camelCase exitCode is honored too', () => {
  const r = formatToolResult(JSON.stringify({ stdout: 'x', exitCode: 1 }))
  assert.equal(r.kind, 'terminal')
  assert.equal(r.exitCode, 1)
})

test('common envelope unwraps to the inner terminal payload', () => {
  const r = formatToolResult(
    JSON.stringify({ result: JSON.stringify({ stdout: 'nested ok', exit_code: 0 }) })
  )
  assert.equal(r.kind, 'terminal')
  assert.equal(r.stdout, 'nested ok')
})

test('single-key content envelope unwraps to inner text', () => {
  const r = formatToolResult(JSON.stringify({ content: 'just a message' }))
  assert.equal(r.kind, 'text')
  assert.equal(r.text, 'just a message')
})

test('a plain object becomes structured key/value entries', () => {
  const r = formatToolResult(JSON.stringify({ path: '/data/x', bytes: 12 }))
  assert.equal(r.kind, 'structured')
  const byKey = Object.fromEntries(r.entries.map(e => [e.key, e.value]))
  assert.equal(byKey.path, '/data/x')
  assert.equal(byKey.bytes, '12')
})

test('object with content AND siblings is structured, not unwrapped', () => {
  const r = formatToolResult(JSON.stringify({ content: 'hi', count: 3 }))
  assert.equal(r.kind, 'structured')
  assert.equal(r.entries.length, 2)
})

test('empty / null output is empty text', () => {
  assert.deepEqual(formatToolResult(''), { kind: 'text', text: '', truncated: false })
  assert.deepEqual(formatToolResult(null), { kind: 'text', text: '', truncated: false })
})

test('oversized field is truncated with a flag', () => {
  const big = 'x'.repeat(30000)
  const r = formatToolResult(JSON.stringify({ stdout: big, exit_code: 0 }))
  assert.equal(r.kind, 'terminal')
  assert.equal(r.truncated, true)
  assert.ok(r.stdout.length < big.length)
})

test('deeply nested wrappers terminate without throwing', () => {
  let v = JSON.stringify({ stdout: 'deep', exit_code: 0 })
  for (let i = 0; i < 8; i++) v = JSON.stringify({ result: v })
  const r = formatToolResult(v)
  // Bounded recursion: it stops unwrapping but never throws and stays a valid shape.
  assert.ok(['terminal', 'text', 'structured'].includes(r.kind))
})

test('terminal keys alongside OTHER payload stay structured (no data loss)', () => {
  const r = formatToolResult(
    JSON.stringify({ stdout: 'x', files: ['a', 'b'], count: 5 })
  )
  assert.equal(r.kind, 'structured')
  const keys = r.entries.map(e => e.key)
  assert.ok(keys.includes('files'))
  assert.ok(keys.includes('count'))
  assert.ok(keys.includes('stdout'))
})

test('a silent-success terminal is still terminal, with empty streams', () => {
  const r = formatToolResult(JSON.stringify({ stdout: '', stderr: '', exit_code: 0 }))
  assert.equal(r.kind, 'terminal')
  assert.equal(r.stdout, '')
  assert.equal(r.stderr, '')
  assert.equal(r.exitCode, 0)
})

test('a single-key wrapper around null is empty text, not "null"', () => {
  assert.deepEqual(
    formatToolResult(JSON.stringify({ content: null })),
    { kind: 'text', text: '', truncated: false }
  )
  assert.deepEqual(
    formatToolResult(JSON.stringify({ data: null })),
    { kind: 'text', text: '', truncated: false }
  )
})

test('a structured value is truncated and flags it', () => {
  const big = 'y'.repeat(30000)
  const r = formatToolResult(JSON.stringify({ path: '/x', blob: big }))
  assert.equal(r.kind, 'structured')
  assert.equal(r.truncated, true)
  const blob = r.entries.find(e => e.key === 'blob')
  assert.ok(blob.value.length < big.length)
})

test('Claude Bash "Exit code N" failure prefix parses to a terminal', () => {
  const r = formatToolResult('Exit code 1\ncat: /x: No such file or directory')
  assert.equal(r.kind, 'terminal')
  assert.equal(r.exitCode, 1)
  assert.equal(r.stderr, 'cat: /x: No such file or directory')
  assert.equal(r.stdout, '')
})

test('plain stdout (a successful bash) is NOT treated as a failure', () => {
  const r = formatToolResult('hello world\n/data')
  assert.equal(r.kind, 'text')
  assert.equal(toolResultFailed('hello world\n/data'), false)
})

test('the Exit-code prefix is a real failure signal for toolResultFailed', () => {
  assert.equal(toolResultFailed('Exit code 127\ncommand not found'), true)
})

test('toolResultFailed: true only for a nonzero terminal exit', () => {
  assert.equal(toolResultFailed(JSON.stringify({ stdout: '', stderr: 'x', exit_code: 127 })), true)
  assert.equal(toolResultFailed(JSON.stringify({ stdout: 'ok', exit_code: 0 })), false)
  assert.equal(toolResultFailed('plain text, not a terminal'), false)
  assert.equal(toolResultFailed(JSON.stringify({ path: '/x', bytes: 1 })), false)
  assert.equal(toolResultFailed(null), false)
})

test('toolBlockExitCode: explicit output_exit_code field wins over a parse', () => {
  // A reduced block (contract rule 6) carries output_exit_code; the inline text
  // is only a carved excerpt, so the field is authoritative.
  assert.equal(toolBlockExitCode({ output_exit_code: 1, output: 'carved…excerpt' }), 1)
  assert.equal(toolBlockExitCode({ output_exit_code: 0, output: 'anything' }), 0)
})

test('toolBlockExitCode: falls back to parsing the output when no field', () => {
  assert.equal(
    toolBlockExitCode({ output: JSON.stringify({ stderr: 'x', exit_code: 137 }) }),
    137,
  )
  assert.equal(toolBlockExitCode({ output: 'plain text' }), null)
  assert.equal(toolBlockExitCode({}), null)
})

test('toolBlockFailed: field-or-parse failure detection survives truncation', () => {
  // Field path: a carved excerpt that no longer parses still reads failed.
  assert.equal(toolBlockFailed({ output_exit_code: 2, output: 'head…[X B]…tail' }), true)
  assert.equal(toolBlockFailed({ output_exit_code: 0, output: 'head…tail' }), false)
  // Parse path: a preserved "Exit code N" head still detects the failure.
  assert.equal(toolBlockFailed({ output: 'Exit code 1\nboom' }), true)
  assert.equal(toolBlockFailed({ output: 'all good' }), false)
})
