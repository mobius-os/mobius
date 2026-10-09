import { test } from 'node:test'
import assert from 'node:assert/strict'
import { Marked } from 'marked'
import {
  splitSteerMarkdown, markdownRangeTokens, markdownRangeSource, sliceMarkdownRange,
} from '../markdown/steerMarkdownRange.js'

function visible(tokens) {
  return tokens.map(token => {
    if (token.tokens && (token.type === 'paragraph' || token.type === 'heading'
      || token.type === 'strong' || token.type === 'em' || token.type === 'del')) {
      return visible(token.tokens)
    }
    return token.text ?? ''
  }).join('')
}

test('a steer inside strong keeps formatting and every visible character exactly once', () => {
  const source = '**3. Don’t confuse uncertainty with failure—or success.**'
  const cut = source.indexOf('uncertainty') + 5
  const split = splitSteerMarkdown(source, cut)
  assert.ok(split)
  assert.equal(split.before.source.slice(split.before.start, split.before.end)
    + split.after.source.slice(split.after.start, split.after.end), source)
  assert.equal(visible(markdownRangeTokens(split.before))
    + visible(markdownRangeTokens(split.after)), '3. Don’t confuse uncertainty with failure—or success.')
  assert.equal(markdownRangeTokens(split.before)[0].tokens[0].type, 'strong')
  assert.equal(markdownRangeTokens(split.after)[0].tokens[0].type, 'strong')
  assert.notEqual(markdownRangeTokens(split.before)[0].raw, markdownRangeTokens(split.after)[0].raw)
})

test('whole range copy retains original image/link atoms and balances nested formatting', () => {
  const atom = '![diagram](/api/chats/fixture/media/diagram.png "Source title") [docs](https://example.com/docs)'
  const source = `${atom}\n\n## A **bold *nested* ~~ending~~** tail`
  const split = splitSteerMarkdown(source, source.indexOf('nested') + 3)
  const md = new Marked()
  for (const range of [split.before, split.after]) {
    const copied = markdownRangeSource(range)
    assert.equal(md.parse(copied), md.parser(markdownRangeTokens(range)),
      'normal paste must preserve the projected formatting, not unmatched raw delimiters')
  }
  assert.ok(markdownRangeSource(split.before).startsWith(atom))
  assert.equal(markdownRangeSource(split.after), '## ***ted* ~~ending~~** tail\n')
})

test('clipped setext headings copy as equivalent standalone headings', () => {
  const source = 'A **nested heading**\n===================='
  const split = splitSteerMarkdown(source, source.indexOf('heading') + 3)
  const md = new Marked()
  for (const range of [split.before, split.after]) {
    assert.equal(md.parse(markdownRangeSource(range)), md.parser(markdownRangeTokens(range)))
  }
})

test('numeric and named entities retain their rendered meaning in copied ranges', () => {
  const md = new Marked()
  for (const entity of ['&#42;', '&#x2A;', '&#35;', '&#128512;', '&amp;']) {
    const source = `**Number ${entity}** then **Planned**`
    const split = splitSteerMarkdown(source, source.indexOf('Planned') + 4)
    for (const range of [split.before, split.after]) {
      assert.equal(md.parse(markdownRangeSource(range)), md.parser(markdownRangeTokens(range)))
    }
  }
})

test('outer italics retain their source delimiter around nested bold', () => {
  const split = splitSteerMarkdown('_ab **cd**ef_', 2)
  const md = new Marked()
  for (const range of [split.before, split.after]) {
    assert.equal(md.parse(markdownRangeSource(range)), md.parser(markdownRangeTokens(range)))
  }
})

test('hard-break atoms cannot escape a synthetic closing emphasis marker', () => {
  const md = new Marked()
  for (const breakSource of ['\\\n', '  \n']) {
    const source = `**foo${breakSource}bar baz**`
    const boundary = splitSteerMarkdown(source, source.indexOf('bar'))
    assert.equal(md.parse(markdownRangeSource(boundary.before).trim()), '<p><strong>foo</strong></p>\n',
      'whole-block clipboard trimming may remove a terminal break, never the formatting')
    const interior = splitSteerMarkdown(source, source.indexOf('bar') + 2)
    for (const range of [interior.before, interior.after]) {
      assert.equal(md.parse(markdownRangeSource(range)), md.parser(range.tokens),
        'line breaks inside copied text keep their rendered meaning')
    }
  }
})

test('copy moves boundary whitespace outside clipped emphasis without losing characters', () => {
  const source = '__Start middle end__'
  const split = splitSteerMarkdown(source, source.indexOf('middle'))
  assert.equal(markdownRangeSource(split.before), '__Start__ ')
  assert.equal(markdownRangeSource(split.after), '__middle end__')
  const middle = sliceMarkdownRange(split.after, 'middle'.length, 'middle end'.length)
  assert.equal(markdownRangeSource(middle), ' __end__')
  assert.equal(new Marked().parse(markdownRangeSource(middle)), '<p> <strong>end</strong></p>\n')
})

test('clipping adjacent nested emphasis does not turn it into bold on paste', () => {
  const source = 'A **escape \\*literal* and `code` end** tail'
  const split = splitSteerMarkdown(source, source.indexOf('escape') + 3)
  const md = new Marked()
  assert.equal(md.parse(markdownRangeSource(split.before)), '<p>A <em>esc</em></p>\n')
  assert.equal(markdownRangeSource(split.before), 'A *esc*')
  const suffix = splitSteerMarkdown(source, source.length - '* tail'.length).after
  assert.equal(md.parse(markdownRangeSource(suffix)), '<p>* tail</p>\n')
})

for (const [source, tag] of [['**ab **cde**fg**', 'strong'], ['~~ab ~~cde~~fg~~', 'del']]) {
  test(`copying clipped nested ${tag} preserves formatting instead of colliding delimiters`, () => {
    const split = splitSteerMarkdown(source, 5)
    assert.ok(split)
    assert.equal(new Marked().parse(markdownRangeSource(split.after)), `<p><${tag}>cdefg</${tag}></p>\n`)
    assert.equal(new Marked().parse(markdownRangeSource(split.before)), `<p><${tag}>ab</${tag}> </p>\n`)
  })
}

test('every accepted fixture cut copies the same effective formatting and source atoms', () => {
  // Redundant nested strong/em/del have one effective style in the renderer.
  // Space may move outside emphasis on paste; meaningful characters must not.
  function characters(tokens, formats = []) {
    return tokens.flatMap(token => {
      const style = ['strong', 'em', 'del', 'list', 'codespan'].includes(token.type)
        ? token.type : token.type === 'heading' ? `h${token.depth}`
          : ['link', 'image'].includes(token.type) ? `${token.type}:${token.href}` : null
      const next = style ? [...new Set([...formats, style])].sort() : formats
      if (token.items) return token.items.flatMap(item => characters(item.tokens, next))
      if (token.tokens) return characters(token.tokens, next)
      return Array.from(token.text ?? '').filter(char => !/\s/.test(char))
        .map(char => ({ char, formats: next }))
    })
  }
  const md = new Marked()
  for (const source of [
    '**ab **cde**fg**', '~~ab ~~cde~~fg~~', '*ab *cde*fg*', '***bold and italic***',
    '**ab *cd **ef** gh* ij**', '~~ab **cd ~~ef~~ gh** ij~~',
    '_ab **cd**ef_', '*ab __cd__ ef*', 'x*a**bc**d*y', '__ab *cd*ef__',
    '![diagram](/api/media/source.png) **Plan *nested* ~~ending~~ tail**',
    'A **escape \\*literal* and `code` end** tail',
    '## A **bold *nested* ~~ending~~** tail', 'A **heading**\n=============',
  ]) {
    for (let cut = 1; cut < source.length; cut++) {
      const split = splitSteerMarkdown(source, cut)
      if (!split) continue
      for (const range of [split.before, split.after]) {
        assert.deepEqual(characters(md.lexer(markdownRangeSource(range))),
          characters(markdownRangeTokens(range)), `${source} at ${cut}`)
      }
    }
  }
})

test('reference atoms copy as self-contained source targets without unrelated definitions', () => {
  const md = new Marked()
  for (const use of ['[ref][foo]', '[foo][]', '[foo]', '![diagram][foo]', '![foo][]', '![foo]']) {
    const source = `A ${use} **bold end**\n\n[foo]: https://example.com/source "Title"\n[unrelated]: https://example.com/hidden`
    const split = splitSteerMarkdown(source, source.indexOf('bold') + 2)
    const copied = markdownRangeSource(split.before)
    assert.equal(md.parse(copied), md.parser(markdownRangeTokens(split.before)), use)
    assert.ok(!copied.includes('example.com/hidden'), 'do not copy definitions for unselected atoms')
    assert.ok(copied.includes('[foo]: https://example.com/source "Title"'))
  }
})

test('whole earlier blocks and relative slices retain only their needed source references', () => {
  const md = new Marked()
  for (const [use, definition] of [
    ['[ref][ Foo   Bar ]', '[foo bar]: <https://example.com/a b> "Source \\"title\\""'],
    ['[ß]', '[SS]: https://example.com/unicode'],
    ['![diagram][fo\\[o]', '[fo\\[o]: /api/media/source.png'],
  ]) {
    const source = `${use}\n\n**abcdef**\n\n${definition}\n[unrelated]: https://example.com/hidden`
    const split = splitSteerMarkdown(source, source.indexOf('abcdef') + 3)
    const copied = markdownRangeSource(split.before)
    assert.equal(md.parse(copied), md.parser(markdownRangeTokens(split.before)))
    assert.ok(copied.includes(definition), 'copy uses the exact original definition')
    assert.ok(!copied.includes('example.com/hidden'))
    const sliced = sliceMarkdownRange(split.before, 0, use.length)
    assert.equal(md.parse(markdownRangeSource(sliced)), md.parse(`${use}\n\n${definition}`))
    assert.ok(!markdownRangeSource(split.after).includes('example.com/hidden'))
  }
})

test('nested emphasis and deletion survive projection; unrelated whole blocks stay intact', () => {
  const source = 'Prelude\n\n## A **bold *nested* ~~ending~~** tail'
  const cut = source.indexOf('nested') + 3
  const split = splitSteerMarkdown(source, cut)
  assert.ok(split)
  assert.equal(visible(markdownRangeTokens(split.before))
    + visible(markdownRangeTokens(split.after)), 'PreludeA bold nested ending tail')
  const heading = markdownRangeTokens(split.before).at(-1)
  assert.equal(heading.type, 'heading')
  assert.equal(heading.depth, 2)
  assert.equal(heading.tokens[1].type, 'strong')
  assert.equal(heading.tokens[1].tokens[1].type, 'em')
  assert.equal(markdownRangeTokens(split.after)[0].tokens[0].type, 'strong')
})

test('an unfinished delimiter remains literal text from the full replay', () => {
  const source = 'hello **unfinished'
  const split = splitSteerMarkdown(source, source.indexOf('finished'))
  assert.ok(split)
  assert.equal(visible(markdownRangeTokens(split.before))
    + visible(markdownRangeTokens(split.after)), source)
})

test('fails closed on unsupported crossings and invalid source boundaries', () => {
  for (const source of ['[label](https://example.com)', '`code here`', '$x+y$',
    '<span>hello</span>', '- list item', '| a | b |\n|---|---|\n| c | d |']) {
    assert.equal(splitSteerMarkdown(source, Math.floor(source.length / 2)), null, source)
  }
  assert.equal(splitSteerMarkdown('a👩‍💻b', 2), null)
  assert.equal(splitSteerMarkdown('a &amp; b', 4), null)
  assert.equal(splitSteerMarkdown('plain', 0), null)
  assert.equal(splitSteerMarkdown('plain', 99), null)
})

test('range slicing is relative, immutable, and still projects from original parse', () => {
  const source = '**abcdef**'
  const split = splitSteerMarkdown(source, 5)
  assert.ok(split)
  const original = JSON.stringify(split.after)
  const sliced = sliceMarkdownRange(split.after, 0, 2)
  assert.ok(sliced)
  assert.equal(sliced.start, split.after.start)
  assert.equal(sliced.end, split.after.start + 2)
  assert.equal(visible(markdownRangeTokens(sliced)), 'de')
  assert.equal(markdownRangeTokens(sliced)[0].tokens[0].type, 'strong')
  assert.equal(JSON.stringify(split.after), original)
  assert.equal(sliceMarkdownRange(split.after, -1, 2), null)
  assert.equal(sliceMarkdownRange(split.after, 0, 99), null)
  assert.equal(sliceMarkdownRange(split.after, 3, 4), null)
})
