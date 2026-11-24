import assert from 'node:assert/strict'
import test from 'node:test'
import {
  addProjectCsvColumn,
  addProjectCsvRow,
  parseProjectCsv,
  serializeProjectCsv,
  updateProjectCsvCell,
} from '../projectFormats.js'

test('CSV project source round-trips quoted commas, quotes, and newlines', () => {
  const source = 'Name,Note\r\nAda,"one, two"\r\nLin,"said ""hello""\nand left"\r\n'
  const rows = parseProjectCsv(source)
  assert.deepEqual(rows, [
    ['Name', 'Note'],
    ['Ada', 'one, two'],
    ['Lin', 'said "hello"\nand left'],
  ])
  assert.equal(serializeProjectCsv(rows), 'Name,Note\nAda,"one, two"\nLin,"said ""hello""\nand left"\n')
})

test('CSV grid mutations preserve a rectangular durable matrix', () => {
  let rows = parseProjectCsv('A,B\n1,2\n')
  rows = updateProjectCsvCell(rows, 1, 1, '3')
  rows = addProjectCsvRow(rows)
  rows = addProjectCsvColumn(rows)
  assert.deepEqual(rows, [
    ['A', 'B', ''],
    ['1', '3', ''],
    ['', '', ''],
  ])
  assert.equal(serializeProjectCsv(rows), 'A,B,\n1,3,\n,,\n')
})

test('large CSV files parse and add rows without spreading the matrix as arguments', () => {
  const rows = parseProjectCsv('Header\n' + 'x\n'.repeat(150_000))
  assert.equal(rows.length, 150_001)
  assert.deepEqual(rows[0], ['Header'])
  assert.deepEqual(rows.at(-1), ['x'])
  const extended = addProjectCsvRow(rows)
  assert.equal(extended.length, rows.length + 1)
  assert.deepEqual(extended.at(-1), [''])
})
