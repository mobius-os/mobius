function delimiterAtStart(src) {
  if (!src.startsWith('$')) return ''
  if (src.startsWith('$$$')) return ''
  return src.startsWith('$$') ? '$$' : '$'
}

function inlineToken(src) {
  const delimiter = delimiterAtStart(src)
  if (!delimiter) return undefined

  for (let i = delimiter.length; i < src.length; i += 1) {
    if (src[i] === '\n') return undefined
    if (src[i] === '\\') {
      i += 1
      continue
    }
    if (!src.startsWith(delimiter, i)) continue
    const text = src.slice(delimiter.length, i)
    if (!text || text.endsWith('$')) return undefined
    return {
      type: 'inlineKatex',
      raw: src.slice(0, i + delimiter.length),
      text: text.trim(),
      displayMode: delimiter.length === 2,
    }
  }
  return undefined
}

function blockToken(src) {
  const delimiter = delimiterAtStart(src)
  if (!delimiter || src[delimiter.length] !== '\n') return undefined

  const contentStart = delimiter.length + 1
  let closingStart = src.indexOf(`\n${delimiter}`, contentStart)
  while (closingStart !== -1) {
    const closingEnd = closingStart + 1 + delimiter.length
    const next = src[closingEnd]
    if ((next === '\n' || next === undefined) && closingStart > contentStart) {
      return {
        type: 'blockKatex',
        raw: src.slice(0, next === '\n' ? closingEnd + 1 : closingEnd),
        text: src.slice(contentStart, closingStart).trim(),
        displayMode: delimiter.length === 2,
      }
    }
    closingStart = src.indexOf(`\n${delimiter}`, closingStart + 1)
  }
  return undefined
}

/** Marked tokenizers for math delimiters. Rendering is deliberately absent:
 * KaTeX is loaded only if one of these tokens reaches the React renderer. */
export function mathTokens() {
  return {
    extensions: [
      {
        name: 'inlineKatex',
        level: 'inline',
        start(src) {
          let index = src.indexOf('$')
          while (index !== -1) {
            if (inlineToken(src.slice(index))) return index
            index = src.indexOf('$', index + 1)
          }
          return undefined
        },
        tokenizer: inlineToken,
      },
      {
        name: 'blockKatex',
        level: 'block',
        tokenizer: blockToken,
      },
    ],
  }
}
