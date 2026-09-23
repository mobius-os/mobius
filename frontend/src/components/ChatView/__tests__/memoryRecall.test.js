import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import {
  noteHref,
  noteLabel,
  safeAppSlug,
  safeNoteId,
} from '../memoryRecall.js'
import { memoryRecallCardModel } from '../memoryRecallCard.js'
import MemoryRecallCard from '../MemoryRecallCard.jsx'
import { _resetDisclosureStateForTests } from '../disclosureState.js'

const note = (id, extra = {}) => ({
  id,
  path: `notes/${id}.md`,
  title: id.replace(/-/g, ' '),
  ...extra,
})

test('only a well-formed note id may build a deep link', () => {
  assert.equal(safeNoteId('theme-variables-are-shared'), 'theme-variables-are-shared')
  assert.equal(safeNoteId('../../etc/passwd'), '')
  assert.equal(safeNoteId('notes/alpha'), '')
  assert.equal(safeNoteId('a b'), '')
  assert.equal(safeNoteId(''), '')
  assert.equal(safeNoteId(null), '')
  assert.equal(safeNoteId('x'.repeat(200)), '')
})

test('a note links into the Memory app through the shell intent contract', () => {
  assert.equal(
    noteHref({ id: 'theme-variables-are-shared' }),
    '/shell/?app=memory&intent=note%3Atheme-variables-are-shared',
  )
  assert.equal(noteHref({ id: '../evil' }), '',
    'an unsafe id yields no link rather than an unsafe one')
  assert.equal(
    noteHref({ id: 'alpha', app_slug: 'memory-2' }),
    '/shell/?app=memory-2&intent=note%3Aalpha',
    'a suffixed official install links to the app that performed the recall',
  )
  assert.equal(safeAppSlug('memory-12'), 'memory-12')
  assert.equal(noteHref({ id: 'alpha', app_slug: '../memory' }), '',
    'a present but invalid app slug fails closed')
})

test('a note without a title still reads as words, never blank', () => {
  assert.equal(noteLabel({ id: 'x', title: 'Real Title' }), 'Real Title')
  assert.equal(noteLabel({ id: 'theme-variables-are-shared' }),
    'theme variables are shared')
  assert.equal(noteLabel({ id: '../evil' }), '')
})

test('the card model keeps the question and result summaries together', () => {
  const model = memoryRecallCardModel({
    status: 'hit',
    query: '  What did we decide\nabout navigation?  ',
    app_slug: 'memory',
    notes: [
      note('alpha', { title: 'Use one navigation seam',
        excerpt: 'All internal links should use the shell intent contract.' }),
      note('beta', { title: 'Keep destinations durable' }),
    ],
  })

  assert.equal(model.query, 'What did we decide about navigation?')
  assert.equal(model.notes.length, 2)
  assert.equal(model.notes[0].label, 'Use one navigation seam')
  assert.equal(
    model.notes[0].summary,
    'All internal links should use the shell intent contract.',
  )
  assert.equal(model.notes[0].href,
    '/shell/?app=memory&intent=note%3Aalpha')
})

test('the V2 card renders app-owned bounded display copy', () => {
  const model = memoryRecallCardModel({
    status: 'hit',
    display: {
      label: 'Reused Memory search — 19 relevant notes',
      detail: 'Showing 2 of 19 relevant notes; more catalogue entries are available.',
      warning: 'Discovery stopped at a safety boundary; this catalogue may be incomplete.',
    },
    notes: [note('alpha'), note('beta')],
  })
  assert.match(model.detail, /Showing 2 of 19/)
  assert.match(model.warning, /may be incomplete/)
})

test('the platform does not interpret Memory pagination fields', () => {
  const model = memoryRecallCardModel({
    status: 'hit',
    phase: 'read',
    page: { requested_count: 2, fully_supplied_count: 1,
      complete: false, next_cursor: 'body:hash:1:0' },
    display: { label: 'Read a Memory page' },
    notes: [note('alpha')],
  })
  assert.equal('phase' in model, false)
  assert.equal('hasMore' in model, false)
})

test('the expanded card preserves every note in the bounded receipt', () => {
  const notes = Array.from({ length: 12 }, (_, index) => note(`note-${index}`))
  const model = memoryRecallCardModel({ status: 'hit', notes })
  assert.equal(model.notes.length, 12)
  assert.equal(model.notes.at(-1).label, 'note 11')
})

test('an empty incomplete lookup renders its warning', { concurrency: false }, () => {
  const previousStorage = globalThis.sessionStorage
  globalThis.sessionStorage = {
    getItem: () => JSON.stringify(['empty-warning']),
    setItem: () => {},
  }
  _resetDisclosureStateForTests()
  try {
    const warning = 'Discovery stopped at a safety boundary; this catalogue may be incomplete.'
    const html = renderToStaticMarkup(createElement(MemoryRecallCard, {
      t: {
        tool: 'Bash',
        status: 'done',
        recall: {
          status: 'empty',
          display: {
            label: 'Searched Memory — nothing relevant',
            detail: 'Nothing relevant is recorded yet.',
            warning,
          },
        },
      },
      chatId: 'chat-1',
      disclosureKey: 'empty-warning',
    }))
    assert.match(html, /Nothing relevant is recorded yet\./)
    assert.match(html, /catalogue may be incomplete/)
  } finally {
    if (previousStorage === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previousStorage
    _resetDisclosureStateForTests()
  }
})

test('malformed notes are skipped without dropping valid siblings', () => {
  const model = memoryRecallCardModel({
    status: 'hit',
    notes: [{ id: '../evil', path: 'notes/evil.md' }, note('alpha'), null],
  })
  assert.deepEqual(model.notes.map(item => item.label), ['alpha'])
})

test('searching, empty, and failed recalls remain honest card states', () => {
  for (const status of ['searching', 'empty', 'failed']) {
    assert.equal(memoryRecallCardModel({ status, query: 'q' }).status, status)
  }
  assert.equal(memoryRecallCardModel({ status: 'unknown' }), null)
  assert.equal(memoryRecallCardModel(null), null)
})
