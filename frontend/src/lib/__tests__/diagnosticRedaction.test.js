import test from 'node:test'
import assert from 'node:assert/strict'

import { redactDiagnosticText } from '../diagnosticRedaction.js'

test('diagnostic redaction removes common credentials', () => {
  const input = [
    'https://user:password@example.com/path?token=secret-token&code=secret-code',
    'postgres://db-user:db-password@database.internal/mobius',
    'Authorization: Bearer abc.def.ghi',
    'Cookie: session=secret-cookie',
    'OPENAI_API_KEY=sk-secret',
    'AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE',
    'AWS_SECRET_ACCESS_KEY="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"',
    '{"AWS_SECRET_ACCESS_KEY":"json/secret+value-that-must-not-pass"}',
    'NPM_TOKEN=npm_0123456789abcdefghijklmnopqrstuvwxyz',
    'unlabeled github_pat_0123456789abcdefghijklmnopqrstuvwxyz',
    'opaque c29tZS12ZXJ5LWxvbmctcHJpdmF0ZS12YWx1ZS0xMjM0NTY3ODkw',
    'eyJheader.eyJpayload.signature',
    '-----BEGIN PRIVATE KEY-----\nprivate-key-body\n-----END PRIVATE KEY-----',
  ].join('\n')
  const redacted = redactDiagnosticText(input)

  for (const secret of [
    'password',
    'db-password',
    'secret-token',
    'secret-code',
    'abc.def.ghi',
    'secret-cookie',
    'sk-secret',
    'AKIAIOSFODNN7EXAMPLE',
    'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
    'json/secret+value-that-must-not-pass',
    'npm_0123456789abcdefghijklmnopqrstuvwxyz',
    'github_pat_0123456789abcdefghijklmnopqrstuvwxyz',
    'c29tZS12ZXJ5LWxvbmctcHJpdmF0ZS12YWx1ZS0xMjM0NTY3ODkw',
    'eyJpayload',
    'private-key-body',
  ]) assert.equal(redacted.includes(secret), false, `must redact ${secret}`)
  assert.match(redacted, /\[redacted\]/)
  assert.match(redacted, /\[redacted-private-key\]/)
  assert.match(redacted, /\[redacted-provider-token\]/)
  assert.match(redacted, /\[redacted-high-entropy-value\]/)
})

test('diagnostic redaction preserves ordinary hashes, prose, and long symbols', () => {
  const commit = '8fe8a7ae35dce88ee4af7585b9ff0b4c229df257'
  const symbol = 'VeryLongRecoveryBoundaryComponentIdentifier'
  const diagnostic = `Build ${commit} failed in ${symbol} while rendering the recovery screen.`
  assert.equal(redactDiagnosticText(diagnostic), diagnostic)
})
