import test from 'node:test'
import assert from 'node:assert/strict'
import { mathTokens } from '../markdown/mathTokens.js'

const [inline, block] = mathTokens().extensions

test('math tokenizer preserves inline and display delimiters', () => {
  assert.deepEqual(inline.tokenizer('$x + 1$ after'), {
    type: 'inlineKatex',
    raw: '$x + 1$',
    text: 'x + 1',
    displayMode: false,
  })
  assert.deepEqual(inline.tokenizer('$$x + 1$$ after'), {
    type: 'inlineKatex',
    raw: '$$x + 1$$',
    text: 'x + 1',
    displayMode: true,
  })
})

test('math tokenizer keeps escaped and unmatched delimiters as text', () => {
  assert.equal(inline.tokenizer('$x \\$ y$ after')?.text, 'x \\$ y')
  assert.equal(inline.tokenizer('$not closed'), undefined)
  assert.equal(inline.start('price $5 without a close'), undefined)
})

test('math tokenizer preserves fenced block math', () => {
  assert.deepEqual(block.tokenizer('$$\nx + 1\n$$\nafter'), {
    type: 'blockKatex',
    raw: '$$\nx + 1\n$$\n',
    text: 'x + 1',
    displayMode: true,
  })
  assert.equal(block.tokenizer('$$\nx + 1'), undefined)
})
