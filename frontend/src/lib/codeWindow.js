/**
 * Head window of a large code file, cut on a line boundary.
 *
 * The finder previews big files by their head instead of janking the tab with
 * a full render (deepseek-harness ReadBlock pattern). Cutting at a newline —
 * not mid-line at the raw character cap — is what lets the preview show real
 * line numbers and an honest "showing N of M lines" instead of a character
 * count, and keeps the last visible line from being silently half a line.
 */
export function windowedCode(content, maxChars) {
  const text = String(content ?? '')
  const totalLines = text.length === 0 ? 0 : text.split('\n').length
  if (text.length <= maxChars) {
    return { text, totalLines, shownLines: totalLines, windowed: false }
  }
  const cut = text.lastIndexOf('\n', maxChars)
  // A single enormous line has no boundary to respect; fall back to the cap.
  const head = cut > 0 ? text.slice(0, cut) : text.slice(0, maxChars)
  return {
    text: head,
    totalLines,
    shownLines: head.split('\n').length,
    windowed: true,
  }
}

/** "1\n2\n…\ncount" for a line-number gutter beside an unwrapped <pre>. */
export function lineNumbersFor(count) {
  let numbers = ''
  for (let line = 1; line <= count; line += 1) {
    numbers += line === count ? String(line) : `${line}\n`
  }
  return numbers
}
