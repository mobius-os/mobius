import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  applySpeechHints,
  normalizeSpeechInput,
  sanitizeSpeechHints,
} from '../speech/speechDocument.js'


test('speech hints are bounded, deduplicated, and applied at exact word edges', () => {
  const hints = sanitizeSpeechHints([
    { written: 'AI', spoken: 'A I' },
    { written: 'AI Lab', spoken: 'A I laboratory' },
    { written: 'AI', spoken: 'duplicate is ignored' },
    { written: '', spoken: 'empty is ignored' },
    null,
  ])

  assert.deepEqual(hints, [
    { written: 'AI', spoken: 'A I' },
    { written: 'AI Lab', spoken: 'A I laboratory' },
  ])
  assert.equal(
    applySpeechHints('The AI Lab uses AI, not PLAINLY.', hints),
    'The A I laboratory uses A I, not PLAINLY.',
  )
  assert.equal(sanitizeSpeechHints(Array.from({ length: 101 }, (_, index) => ({
    written: `term-${index}`,
    spoken: `term ${index}`,
  }))).length, 100)
})


test('speech input enforces total text and document collection bounds', () => {
  assert.throws(
    () => normalizeSpeechInput({ text: '123456' }, 5),
    (error) => error.code === 'invalid_request' && /cannot exceed 5/.test(error.message),
  )
  assert.throws(
    () => normalizeSpeechInput({
      document: {
        version: 1,
        segments: Array.from({ length: 513 }, () => ({ text: 'x' })),
      },
    }),
    (error) => error.code === 'invalid_request' && /cannot exceed 512 segments/.test(error.message),
  )
  assert.throws(
    () => normalizeSpeechInput({ document: { version: 1, segments: [] } }),
    (error) => error.code === 'invalid_request' && /segments are required/.test(error.message),
  )
  assert.throws(
    () => normalizeSpeechInput({
      document: {
        version: 1,
        hints: Array.from({ length: 101 }, (_, index) => ({
          written: `term-${index}`,
          spoken: `term ${index}`,
        })),
        segments: [{ text: 'Hello' }],
      },
    }),
    (error) => error.code === 'invalid_request' && /cannot exceed 100 hints/.test(error.message),
  )
})


test('Speech Document rejects oversized raw fields before normalizing them', () => {
  const documents = [
    { version: 1, locale: 'x'.repeat(1_025), segments: [{ text: 'Hello' }] },
    {
      version: 1,
      hints: [{ written: 'x'.repeat(161), spoken: 'safe' }],
      segments: [{ text: 'Hello' }],
    },
    {
      version: 1,
      hints: [{ written: 'safe', spoken: 'x'.repeat(241) }],
      segments: [{ text: 'Hello' }],
    },
    { version: 1, segments: [{ kind: 'x'.repeat(1_025), text: 'Hello' }] },
  ]

  for (const document of documents) {
    assert.throws(
      () => normalizeSpeechInput({ document }),
      (error) => error.code === 'invalid_request' && /cannot exceed/.test(error.message),
    )
  }

  assert.throws(
    () => normalizeSpeechInput({ text: '     x' }, 1),
    (error) => error.code === 'invalid_request' && /cannot exceed 1 character/.test(error.message),
  )
  assert.throws(
    () => normalizeSpeechInput({
      document: { version: 1, segments: [{ text: '      ' }] },
    }, 5),
    (error) => error.code === 'invalid_request' && /cannot exceed 5 characters/.test(error.message),
  )
})


test('Speech Document bounds aggregate raw text before hint or whitespace transforms', () => {
  assert.throws(
    () => normalizeSpeechInput({
      document: {
        version: 1,
        hints: [
          { written: 'one', spoken: '1' },
          { written: 'two', spoken: '2' },
        ],
        segments: [{ text: 'one' }, { text: 'two' }],
      },
    }, 5),
    (error) => error.code === 'invalid_request' && /cannot exceed 5/.test(error.message),
  )
})


test('speech hint expansion stops at the text budget instead of building the expanded value', () => {
  const hints = Array.from({ length: 25 }, (_, index) => ({
    written: String.fromCharCode(97 + index),
    spoken: `${String.fromCharCode(98 + index)} ${String.fromCharCode(98 + index)}`,
  }))

  assert.throws(
    () => applySpeechHints('a', hints, 1_000),
    (error) => error.code === 'invalid_request' && /cannot exceed 1,000/.test(error.message),
  )
})


test('Speech Document fields keep their declared types instead of being coerced', () => {
  const invalidDocuments = [
    { version: 1, locale: 42, segments: [{ text: 'Hello' }] },
    { version: 1, hints: {}, segments: [{ text: 'Hello' }] },
    {
      version: 1,
      hints: [{ written: 42, spoken: 'forty two' }],
      segments: [{ text: 'Hello' }],
    },
    { version: 1, segments: [null, { text: 'Hello' }] },
    { version: 1, segments: [{ text: { value: 'Hello' } }] },
    { version: 1, segments: [{ text: 'Hello', kind: false }] },
    { version: 1, segments: [{ text: 'Hello', pauseAfterMs: true }] },
  ]

  for (const document of invalidDocuments) {
    assert.throws(
      () => normalizeSpeechInput({ document }),
      (error) => error.code === 'invalid_request',
    )
  }
})


test('Speech Document segments normalize text, kinds, hints, and pauses once', () => {
  const value = normalizeSpeechInput({
    document: {
      version: 1,
      locale: '  en-GB  ',
      hints: [{ written: 'Möbius', spoken: 'Moh bee us' }],
      segments: [
        { kind: ' summary ', text: '  Möbius\n is ready. ', pauseAfterMs: 812.6 },
        { kind: '', text: 'Second segment.', pauseAfterMs: 99_000 },
        { text: '   ' },
      ],
    },
  })

  assert.deepEqual(value, {
    version: 1,
    locale: 'en-GB',
    hints: [{ written: 'Möbius', spoken: 'Moh bee us' }],
    segments: [
      {
        kind: 'summary',
        text: 'Moh bee us is ready.',
        pauseAfterMs: 813,
      },
      {
        kind: 'paragraph',
        text: 'Second segment.',
        pauseAfterMs: 5_000,
      },
    ],
  })

  const clamped = normalizeSpeechInput({
    document: {
      version: 1,
      locale: 'l'.repeat(50),
      segments: [{ kind: 'k'.repeat(50), text: 'Clamped metadata.' }],
    },
  })
  assert.equal(clamped.locale, 'l'.repeat(35))
  assert.equal(clamped.segments[0].kind, 'k'.repeat(40))
})
