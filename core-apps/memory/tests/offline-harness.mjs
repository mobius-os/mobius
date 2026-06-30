// OFFLINE RENDER HARNESS for app-memory.
// Drives the REAL compiled makeSharedMemoryStore (from the esbuild bundle of
// index.jsx) through the exact two effects the App runs — the graph subscribe
// and the open-note subscribe — with:
//   * a mocked Cache-Storage-style mirror (read-through cache), and
//   * the network forced DOWN after the first prime.
// It reproduces the component's render-state reducer (status + revalidating)
// so the assertions are on the SAME state the UI paints. No DOM needed; the
// data path here is byte-identical to what the iframe runs.
import assert from 'node:assert/strict'
import { makeSharedMemoryStore } from './.build/index.mjs'

const GRAPH = JSON.stringify({
  nodes: [
    { id: 'index', title: 'Memory — Home', type: 'moc', path: 'index.md' },
    { id: 'about-user', title: 'About the user', type: 'note', path: 'notes/about-user.md' },
  ],
  edges: [{ source: 'index', target: 'about-user', kind: 'moc' }],
  problems: [],
})
const NOTE_V1 = '---\ntitle: About the user\n---\n# About the user\nLikes terse prompts.'
const NOTE_V2 = NOTE_V1 + '\n\nUPDATE: an agent appended this line.'

// Mocked Cache Storage mirror (what survives offline).
function makeCacheMirror() {
  const m = new Map()
  return { map: m, read: async (k) => (m.has(k) ? m.get(k) : null), write: async (k, e) => { m.set(k, e) } }
}

// Controllable network. `online=false` => every request throws like a real
// fetch outage. `files` is the server-side truth (an agent write mutates it).
function makeNet(files, state) {
  return async (url) => {
    if (!state.online) throw new TypeError('Failed to fetch (offline)')
    const rel = url.replace('/api/storage/shared/memory/', '')
    if (!(rel in files)) return { ok: false, status: 404, text: async () => '' }
    return { ok: true, status: 200, text: async () => files[rel] }
  }
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms))

async function run() {
  const files = { 'graph.json': GRAPH, 'notes/about-user.md': NOTE_V1 }
  const state = { online: true }
  const cacheStore = makeCacheMirror()
  const net = makeNet(files, state)
  const store = makeSharedMemoryStore({
    getToken: () => 'tok', fetchImpl: net, cacheStore, pollMs: 8, isVisible: () => true,
  })

  // ── Phase 1: ONLINE prime — the App opens once with a network so the cache
  // mirror fills (mirrors the owner's last online session). ──
  // Graph effect render-state reducer (mirror of the App's setGraph/setStatus).
  let graphView = { status: 'loading', nodes: null }
  const unsubGraph = store.subscribe('graph.json', ({ body, present, error }) => {
    if (error && body == null) { graphView = { status: 'error', nodes: null }; return }
    if (!present || body == null) { graphView = { status: 'empty', nodes: [] }; return }
    const data = JSON.parse(body)
    graphView = { status: data.nodes.length ? 'ready' : 'empty', nodes: data.nodes }
  })
  await wait(30)
  assert.equal(graphView.status, 'ready', 'P1: graph rendered online')
  assert.equal(graphView.nodes.length, 2)
  console.log('P1 OK  graph primed online: status=ready nodes=2')

  // Open the "about-user" note (note effect reducer: status + revalidating).
  let noteView = { status: 'loading', md: '', revalidating: false }
  const notePath = 'notes/about-user.md'
  const unsubNote = store.subscribe(
    notePath,
    ({ body, present, error }) => {
      if (error && body == null) { noteView = { ...noteView, status: 'error' }; return }
      if (!present || body == null) { noteView = { ...noteView, status: 'missing' }; return }
      noteView = { ...noteView, status: 'ready', md: body }
    },
    { onRevalidate: (busy) => { noteView = { ...noteView, revalidating: busy } } },
  )
  await wait(30)
  assert.equal(noteView.status, 'ready', 'P1: note rendered online')
  assert.ok(noteView.md.includes('Likes terse prompts'))
  assert.equal(noteView.revalidating, false, 'P1: merging indicator cleared after revalidation')
  console.log('P1 OK  note primed online: status=ready, revalidating cleared')

  unsubGraph(); unsubNote()

  // ── Phase 2: OFFLINE — fresh open of the app with NO network. The store must
  // serve graph + note from the cache mirror. ──
  state.online = false
  let g2 = { status: 'loading', nodes: null }
  const u2g = store.subscribe('graph.json', ({ body, present }) => {
    if (!present || body == null) { g2 = { status: 'empty', nodes: [] }; return }
    const d = JSON.parse(body); g2 = { status: d.nodes.length ? 'ready' : 'empty', nodes: d.nodes }
  })
  let n2 = { status: 'loading', md: '', revalidating: false }
  const revLog = []          // every onRevalidate(bool) — deterministic bracket record
  const mdLog = []           // every body delivered to the note view
  const u2n = store.subscribe(
    notePath,
    ({ body, present }) => {
      if (!present || body == null) { n2 = { ...n2, status: 'missing' }; return }
      n2 = { ...n2, status: 'ready', md: body }; mdLog.push(body)
    },
    { onRevalidate: (b) => { n2 = { ...n2, revalidating: b }; revLog.push(b) } },
  )
  await wait(40)
  assert.equal(g2.status, 'ready', 'P2: GRAPH renders OFFLINE from cache')
  assert.equal(g2.nodes.length, 2)
  assert.equal(n2.status, 'ready', 'P2: NOTE renders OFFLINE from cache')
  assert.ok(n2.md.includes('Likes terse prompts'))
  assert.equal(n2.revalidating, false, 'P2: indicator not stuck on while offline')
  console.log('P2 OK  OFFLINE: graph + note both render from cache, network DOWN')

  // ── Phase 3: external write to the OPEN note path while online again —
  // confirm the note view REPAINTS and the merging indicator clears. ──
  state.online = true
  files['notes/about-user.md'] = NOTE_V2  // an agent rewrote the note on the server
  const revStart = revLog.length
  const deadline = Date.now() + 500
  while (Date.now() < deadline) {
    if (n2.md.includes('an agent appended this line')) break
    await wait(5)
  }
  await wait(30)   // let the closing revalidation bracket settle
  assert.ok(n2.md.includes('an agent appended this line'), 'P3: open note REPAINTED after external agent write')
  const revDuringWrite = revLog.slice(revStart)
  assert.ok(revDuringWrite.includes(true), 'P3: merging indicator turned ON during the pull')
  assert.equal(revLog[revLog.length - 1], false, 'P3: merging indicator CLEARED after fresh body landed')
  assert.equal(n2.revalidating, false, 'P3: final revalidating state is cleared')
  console.log('P3 OK  external write -> note repainted; merging bracket: ' + JSON.stringify(revDuringWrite))

  u2g(); u2n()
  console.log('\nALL OFFLINE-HARNESS PHASES PASSED')
}

run().then(() => process.exit(0)).catch((e) => { console.error('HARNESS FAIL:', e.message); process.exit(1) })
