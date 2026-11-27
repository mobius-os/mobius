import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  highlightSearchTerms,
  searchSnippetPresentation,
  searchTextMatches,
} from '../searchTermHighlight.js'

test('the FTS-marked snippet owns both visible parts and exact destination terms', () => {
  assert.deepEqual(
    searchSnippetPresentation('The \ue000Wombat\ue001 takes the \ue000route map\ue001.'),
    {
      parts: [
        { text: 'The ', marked: false },
        { text: 'Wombat', marked: true },
        { text: ' takes the ', marked: false },
        { text: 'route map', marked: true },
        { text: '.', marked: false },
      ],
      terms: ['Wombat', 'route map'],
    },
  )
})

test('exact marked terms produce case-insensitive, non-overlapping text ranges', () => {
  assert.deepEqual(searchTextMatches('The Wombat route map is open', [
    'wombat', 'route map',
  ]), [
    { start: 4, end: 10, text: 'Wombat' },
    { start: 11, end: 20, text: 'route map' },
  ])
})

test('equivalent Unicode spellings highlight the original text at its real offsets', () => {
  const decomposed = 'Re\u0301union'
  const composed = 'Réunion'
  const source = `📅 ${decomposed}, then ${composed}`
  const secondStart = source.lastIndexOf(composed)
  assert.deepEqual(searchTextMatches(source, [composed]), [
    { start: 3, end: 3 + decomposed.length, text: decomposed },
    { start: secondStart, end: source.length, text: composed },
  ])
  assert.deepEqual(searchTextMatches(composed, [decomposed]), [
    { start: 0, end: composed.length, text: composed },
  ])
})

test('Custom Highlight ranges leave React-owned text nodes intact and stay bounded', () => {
  const source = `needle ${'needle '.repeat(180)}`
  const textNode = {
    nodeValue: source,
    parentElement: { closest: () => null },
  }
  const ranges = []
  const registry = new Map()
  class FakeHighlight {
    constructor(...items) { this.items = items }
  }
  const doc = {
    defaultView: {
      NodeFilter: { SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_REJECT: 2 },
      CSS: { highlights: registry },
      Highlight: FakeHighlight,
    },
    createTreeWalker() {
      let read = false
      return {
        currentNode: null,
        nextNode() {
          if (read) return false
          read = true
          this.currentNode = textNode
          return true
        },
      }
    },
    createRange() {
      const range = {
        setStart(node, offset) { this.start = { node, offset } },
        setEnd(node, offset) { this.end = { node, offset } },
      }
      ranges.push(range)
      return range
    },
  }
  const root = {
    ownerDocument: doc,
    querySelectorAll: selector => selector === '.chat__text' ? [{}] : [],
  }

  const handle = highlightSearchTerms(root, ['needle'])
  assert.equal(textNode.nodeValue, source)
  assert.equal(ranges.length, 128)
  assert.equal(handle.firstRange, ranges[0])
  assert.equal(registry.get('chat-search-result').items.length, 128)
  handle.clear()
  assert.equal(registry.has('chat-search-result'), false)
})
