import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const src = resolve(here, '../..')
const frame = readFileSync(resolve(src, '../public/app-frame.html'), 'utf8')
const canvas = readFileSync(resolve(src, 'components/AppCanvas/AppCanvas.jsx'), 'utf8')
const shell = readFileSync(resolve(src, 'components/Shell/Shell.jsx'), 'utf8')

test('drawer suspension reaches the live app frame before paint', () => {
  // The interactive gate is the FOCUSED canvas minus drawer-open. Post the
  // workspace-reducer refactor (PR2) that is expressed in workspace terms
  // (tabKey === focusedActiveKey) rather than the legacy activeView/activeAppId
  // scalars; the behavior (cancel momentum when the drawer opens) is unchanged.
  assert.match(shell, /interactive=\{tabKey === focusedActiveKey && !drawerOpen\}/)
  // The layout effect's "painted" argument tracks `visible` after the
  // active->visible split (a frame is painted iff it is the active tab of a
  // visible pane); `interactive` stays the focused-pane, drawer-aware gate.
  assert.match(canvas, /useLayoutEffect\(\(\) => \{[\s\S]*sendInteractivity\(swap\.liveVersion, interactive, visible\)/)
  assert.match(canvas, /suspendScrolling:\s*visible\s*&&\s*!enabled/)
  assert.match(canvas, /moebius:frame-interactivity/)
})

test('frame suspension cancels compositor momentum without changing the resting offset', () => {
  assert.match(frame, /function cancelScrollerMomentum\(element\)/)
  assert.match(frame, /element\.scrollTop = top < maxTop \? top \+ 1/)
  assert.match(frame, /element\.scrollTop = top;/)
  assert.match(frame, /data-mobius-frame-suspended/)
  assert.match(frame, /suspendedScrollFrame = requestAnimationFrame\(holdSuspendedScroll\)/)
})
