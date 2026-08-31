/** Durable spreadsheet helpers for Project Finder's CSV surface.
 *
 * The parser handles quoted commas, embedded newlines, and doubled quotes. It
 * deliberately returns a rectangular matrix because a grid editor needs one
 * addressable cell at every row/column coordinate; serialization remains
 * ordinary RFC 4180-style CSV rather than inventing a parallel sheet format.
 */
export function parseProjectCsv(value) {
  const source = String(value ?? '')
  if (!source) return [['']]
  const rows = []
  let row = []
  let cell = ''
  let quoted = false
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index]
    if (quoted) {
      if (char === '"' && source[index + 1] === '"') {
        cell += '"'; index += 1
      } else if (char === '"') quoted = false
      else cell += char
      continue
    }
    if (char === '"' && cell === '') quoted = true
    else if (char === ',') { row.push(cell); cell = '' }
    else if (char === '\n') { row.push(cell); rows.push(row); row = []; cell = '' }
    else if (char !== '\r') cell += char
  }
  if (cell || row.length || !source.endsWith('\n')) {
    row.push(cell); rows.push(row)
  }
  const width = Math.max(1, ...rows.map(item => item.length))
  return rows.map(item => [...item, ...Array(width - item.length).fill('')])
}

function serializeCell(value) {
  const text = String(value ?? '')
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text
}

export function serializeProjectCsv(rows) {
  const matrix = Array.isArray(rows) && rows.length ? rows : [['']]
  return matrix.map(row => (Array.isArray(row) ? row : [row]).map(serializeCell).join(',')).join('\n') + '\n'
}

export function updateProjectCsvCell(rows, rowIndex, columnIndex, value) {
  return rows.map((row, index) => (
    index === rowIndex ? row.map((cell, column) => (column === columnIndex ? value : cell)) : [...row]
  ))
}

export function addProjectCsvRow(rows) {
  const width = Math.max(1, ...(rows || []).map(row => row.length))
  return [...(rows || []), Array(width).fill('')]
}

export function addProjectCsvColumn(rows) {
  return (rows?.length ? rows : [['']]).map(row => [...row, ''])
}
