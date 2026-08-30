import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import FileMentionMenu from '../FileMentionMenu.jsx'
import {
  applyFileMention,
  matchMentionFiles,
  mentionAgentPath,
  mentionQueryFor,
} from '../fileMentions.js'

const files = [
  { name: 'index.html', path: 'index.html', type: 'file' },
  { name: 'main.tex', path: 'src/main.tex', type: 'file' },
  { name: 'style.css', path: 'assets/style.css', type: 'file' },
  { name: 'src', path: 'src', type: 'directory' },
]

test('mentionQueryFor opens on a leading or space-preceded @ and tracks the token', () => {
  assert.deepEqual(mentionQueryFor('@'), { start: 0, query: '' })
  assert.deepEqual(mentionQueryFor('fix @main'), { start: 4, query: 'main' })
  assert.deepEqual(mentionQueryFor('see @src/ma'), { start: 4, query: 'src/ma' })
})

test('mentionQueryFor stays closed for emails, finished tokens, and plain text', () => {
  assert.equal(mentionQueryFor('user@example.com'), null)
  assert.equal(mentionQueryFor('fix @main.tex please'), null)
  assert.equal(mentionQueryFor('no mention here'), null)
  assert.equal(mentionQueryFor(''), null)
})

test('matchMentionFiles ranks basename hits above path-only hits and skips directories', () => {
  const matches = matchMentionFiles('main', files)
  assert.equal(matches[0].path, 'src/main.tex')
  assert.ok(matches.every((file) => file.type !== 'directory'))
})

test('matchMentionFiles offers the head of the listing for an empty query', () => {
  const matches = matchMentionFiles('', files, 2)
  assert.equal(matches.length, 2)
  assert.ok(matches.every((file) => file.type !== 'directory'))
})

test('mentionAgentPath joins the logical project root under /data', () => {
  assert.equal(mentionAgentPath('projects/p1', 'src/main.tex'), '/data/projects/p1/src/main.tex')
  // Rolling-upgrade compatibility rows store an absolute root.
  assert.equal(mentionAgentPath('/data/projects/p2', 'a.html'), '/data/projects/p2/a.html')
})

test('applyFileMention replaces the token with the path and a trailing space', () => {
  const mention = mentionQueryFor('fix @main')
  assert.equal(
    applyFileMention('fix @main', mention, '/data/projects/p1/src/main.tex'),
    'fix /data/projects/p1/src/main.tex ',
  )
  assert.equal(
    applyFileMention('open @draft', mentionQueryFor('open @draft'), '/data/projects/p1/My Draft.md'),
    'open "/data/projects/p1/My Draft.md" ',
  )
})

test('the file picker exposes one active listbox option without stealing textarea focus', () => {
  const html = renderToStaticMarkup(createElement(FileMentionMenu, {
    files: files.filter((file) => file.type === 'file'),
    activeIndex: 1,
    onSelect: () => {},
    listId: 'project-files',
    optionId: 'project-file-active',
  }))
  assert.match(html, /role="listbox"[^>]*aria-label="Project files"/)
  assert.match(html, /id="project-file-active"[^>]*role="option"[^>]*aria-selected="true"/)
  assert.equal((html.match(/aria-selected="true"/g) || []).length, 1)
})
