import test from 'node:test'
import assert from 'node:assert/strict'

import { parseUnifiedDiff } from '../parseUnifiedDiff.js'

const TEXT_DIFF = `diff --git a/src/main.js b/src/main.js
index 1111111..2222222 100644
--- a/src/main.js
+++ b/src/main.js
@@ -1,3 +1,4 @@ function main() {
 keep
-old
+new
+extra
 tail
\\ No newline at end of file
@@ -10,2 +11,2 @@ later
 ten
-eleven
+twelve
diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+first
+second
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
--- a/gone.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-first
-second`

test('parses multiple text files, statuses, and multiple hunks', () => {
  const files = parseUnifiedDiff(TEXT_DIFF)
  assert.equal(files.length, 3)
  assert.deepEqual(files.map(({ path, status }) => ({ path, status })), [
    { path: 'src/main.js', status: 'M' },
    { path: 'new.txt', status: 'A' },
    { path: 'gone.txt', status: 'D' },
  ])
  assert.equal(files[0].hunks.length, 2)
  assert.equal(files[1].oldPath, null)
  assert.equal(files[2].newPath, null)
})

test('tracks old and new line numbers for every line kind', () => {
  const [modified] = parseUnifiedDiff(TEXT_DIFF)
  assert.deepEqual(modified.hunks[0].lines, [
    { type: 'context', text: 'keep', oldNo: 1, newNo: 1 },
    { type: 'del', text: 'old', oldNo: 2, newNo: null },
    { type: 'add', text: 'new', oldNo: null, newNo: 2 },
    { type: 'add', text: 'extra', oldNo: null, newNo: 3 },
    { type: 'context', text: 'tail', oldNo: 3, newNo: 4 },
    { type: 'meta', text: '\\ No newline at end of file', oldNo: null, newNo: null },
  ])
  assert.deepEqual(modified.hunks[1].lines, [
    { type: 'context', text: 'ten', oldNo: 10, newNo: 11 },
    { type: 'del', text: 'eleven', oldNo: 11, newNo: null },
    { type: 'add', text: 'twelve', oldNo: null, newNo: 12 },
  ])
})

test('derives rename, copy, type-change, and binary entries from git headers', () => {
  const files = parseUnifiedDiff(`diff --git a/old name.txt b/new name.txt
similarity index 100%
rename from old name.txt
rename to new name.txt
diff --git a/source.txt b/copy.txt
similarity index 100%
copy from source.txt
copy to copy.txt
diff --git a/link b/link
old mode 100644
new mode 120000
diff --git a/image.png b/image.png
new file mode 100644
index 0000000..1234567
Binary files /dev/null and b/image.png differ`)

  assert.deepEqual(files.map(({ path, oldPath, newPath, status, binary }) => (
    { path, oldPath, newPath, status, binary }
  )), [
    { path: 'new name.txt', oldPath: 'old name.txt', newPath: 'new name.txt', status: 'R', binary: false },
    { path: 'copy.txt', oldPath: 'source.txt', newPath: 'copy.txt', status: 'C', binary: false },
    { path: 'link', oldPath: 'link', newPath: 'link', status: 'T', binary: false },
    { path: 'image.png', oldPath: 'image.png', newPath: 'image.png', status: 'A', binary: true },
  ])
  assert.deepEqual(files[3].hunks, [])
})

test('empty, null, and truncated-tail input is safe', () => {
  assert.deepEqual(parseUnifiedDiff(''), [])
  assert.deepEqual(parseUnifiedDiff(null), [])
  assert.deepEqual(parseUnifiedDiff(undefined), [])

  const [file] = parseUnifiedDiff(`diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-old
+part`)
  assert.equal(file.path, 'a.txt')
  assert.deepEqual(file.hunks[0].lines.at(-1), {
    type: 'add', text: 'part', oldNo: null, newNo: 1,
  })
})


test('a diff-body line starting with "-- "/"++ " does not clobber the file path', () => {
  // A removed "- old comment" is raw "--- old comment"; an added "+ new comment"
  // is raw "+++ new comment". Both must read as content, not as ---/+++ path
  // headers (regression: they clobbered the path, so the modal showed
  // "Diff not shown" for a file whose diff was fully present).
  const [file] = parseUnifiedDiff(`diff --git a/schema.sql b/schema.sql
index 111..222 100644
--- a/schema.sql
+++ b/schema.sql
@@ -1,2 +1,2 @@
 keep
-- old comment
++ new comment`)
  assert.equal(file.path, 'schema.sql')
  assert.equal(file.path, 'schema.sql')
  assert.equal(file.insertions, 1)
  assert.equal(file.deletions, 1)
})
