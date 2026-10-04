/* Discovery stays in the guide; App Store owns access review and installation.
   This module owns the shared catalog hook, the Store-aligned app card used by
   every app screen, and the App Store explainer screen. */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { apiFetch } from '../../api/client.js'
import AppIcon from '../AppIcon.jsx'
import { findAppStoreApp } from '../../lib/appRecovery.js'
import { CheckIcon } from './WalkthroughIcons.jsx'
import { useLoopingTimeline } from './WalkthroughMotion.jsx'
import { Touch, fingerPath } from './WalkthroughTouch.jsx'

const CATALOG_URL = 'https://raw.githubusercontent.com/mobius-os/app-store/main/catalog.json'

function catalogItems(body) {
  if (body?.schema !== 1 || !Array.isArray(body.apps)) throw new Error('App Store listings are unavailable.')
  const items = new Map()
  for (const item of body.apps) {
    if (item && typeof item.id === 'string' && !items.has(item.id)) items.set(item.id, item)
  }
  return items
}

function asDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result)
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(blob)
  })
}

function installedApp(apps, id) {
  return apps.find(app => app.slug === id || app.source_manifest?.id === id) || null
}

/* Loads the Store catalog once (local copy first, then the published one) and
   fetches listing artwork only for the app ids the visible screen asks about. */
export function useStoreCatalog(apps, wantedIds) {
  const storeApp = findAppStoreApp(apps)
  const [catalog, setCatalog] = useState(null)
  const [catalogError, setCatalogError] = useState('')
  const [icons, setIcons] = useState({})

  useEffect(() => {
    let active = true
    const controller = new AbortController()
    async function load() {
      let hasLocal = false
      if (storeApp?.id) {
        try {
          const response = await apiFetch(`/apps/${storeApp.id}/source/file?path=catalog.json`, { signal: controller.signal, timeoutMs: 5000 })
          if (!response.ok) throw new Error('Local App Store catalog unavailable')
          const file = await response.json()
          const local = catalogItems(JSON.parse(file.content))
          if (active) { setCatalog(local); hasLocal = true }
        } catch (error) {
          if (error.name === 'AbortError') return
        }
      }
      try {
        const response = await apiFetch(`/proxy?url=${encodeURIComponent(CATALOG_URL)}`, { signal: controller.signal, timeoutMs: 8000 })
        if (!response.ok) throw new Error('Published App Store catalog unavailable')
        const remote = catalogItems(await response.json())
        if (active) { setCatalog(remote); setCatalogError('') }
      } catch (error) {
        if (active && !hasLocal) setCatalogError('App Store listings are not loading right now. You can explore the full collection in the App Store later.')
      }
    }
    void load()
    return () => { active = false; controller.abort() }
  }, [storeApp?.id])

  const wantedKey = wantedIds.join(',')
  useEffect(() => {
    if (!catalog) return undefined
    let active = true
    const controller = new AbortController()
    for (const id of wantedKey ? wantedKey.split(',') : []) {
      const item = catalog.get(id)
      if (!item?.manifest_url || !item?.raw_base || installedApp(apps, id)) continue
      void (async () => {
        try {
          const response = await apiFetch(`/proxy?url=${encodeURIComponent(item.manifest_url)}`, { signal: controller.signal, timeoutMs: 8000 })
          if (!response.ok) return
          const manifest = await response.json()
          // A renamed app keeps its old listing id in previous_id (Workout was listed as gym).
          if ((manifest.id !== item.id && manifest.previous_id !== item.id) || typeof manifest.icon !== 'string') return
          const icon = new URL(manifest.icon, item.raw_base)
          if (icon.origin !== new URL(item.raw_base).origin || !icon.pathname.startsWith(new URL(item.raw_base).pathname)) return
          const iconResponse = await apiFetch(`/proxy?url=${encodeURIComponent(icon.href)}`, { signal: controller.signal, timeoutMs: 10_000 })
          if (!iconResponse.ok) return
          const image = await iconResponse.blob()
          if (!image.type.startsWith('image/')) return
          const iconData = await asDataUrl(image)
          if (active) setIcons(current => (current[id] ? current : { ...current, [id]: iconData }))
        } catch (_) { /* Initials remain available when listing artwork is offline. */ }
      })()
    }
    return () => { active = false; controller.abort() }
  }, [catalog, wantedKey, apps])

  return useMemo(() => ({ apps, storeApp, catalog, catalogError, icons }), [apps, storeApp, catalog, catalogError, icons])
}

export function StoreAppCard({ app, store, status, onInstall }) {
  const { id, name, blurb } = app
  const stateRef = useRef(null)
  // When Installed replaces the button the focus was on, it follows, so keyboard users keep their place.
  useEffect(() => {
    if (status?.state === 'installed' && document.activeElement === document.body) stateRef.current?.focus({ preventScroll: true })
  }, [status?.state])
  const installed = installedApp(store.apps, id) || (status?.state === 'installed' ? { slug: id } : null)
  const listed = store.catalog?.has(id)
  const busy = status?.state === 'checking' || status?.state === 'installing'
  const label = status?.state === 'checking' ? 'Checking…' : status?.state === 'installing' ? 'Installing…' : status?.state === 'error' ? 'Try again' : listed || !store.catalog ? 'Install' : 'Not listed yet'
  return <article className={`wt-store-card${installed ? ' is-installed' : ''}`}>
    {/* One stable icon identity (the slug) before and after install, so the artwork never flips back to initials. */}
    <AppIcon className="wt-store-card__icon" item={{ slug: id, icon_url: installed?.icon_url || store.icons[id] }} label={name} size={null} />
    <h3>{name}</h3>
    <p>{blurb}</p>
    {installed
      ? <span className="wt-store-card__state" role="status" tabIndex={-1} ref={stateRef}><CheckIcon /> Installed</span>
      : /* aria-disabled, not disabled: the button keeps focus while the access is checked, so focus can come back to it. */
      <button type="button" className="wt-store-card__get" aria-label={`Install ${name}`} aria-disabled={!listed || busy} onClick={() => { if (listed && !busy) onInstall(id) }}>{label}</button>}
  </article>
}

// What the App Store is for, in the order you'd use it. Shown as the same cards as the guide's last screen.
const STORE_POINTS = [
  { id: 'download', label: 'Download', text: 'Browse apps from Möbius and the community.' },
  { id: 'publish', label: 'Publish', text: 'Built something with your agent? Share it so others can install it too.' },
  { id: 'modify', label: 'Modify', text: 'Every app is yours. Ask your agent to change any app you installed.' },
]

// The listings the App Store window below shows, worded like the real Store. Workout is the one that
// gets installed and opened (its Store listing id is `gym`).
export const STORE_WINDOW_APPS = [
  { id: 'habits', name: 'Habits', blurb: 'Build lasting routines with streaks and reminders.', owned: true },
  { id: 'notes', name: 'Notes', blurb: 'Markdown notes with checklists, images, and search.', owned: true },
  { id: 'social', name: 'Social', blurb: 'Message other Möbius people and join the community board.', owned: true },
  { id: 'gym', name: 'Workout', blurb: 'Plan routines, log every set, explore exercises, and follow your progress.', owned: false },
]
// idle · finger glides in and taps Install · installing (finger fades) · installed · finger fades in on Open and taps · app open (ms each)
const MOCK_PHASES = [1400, 1550, 1700, 350, 700, 4200]
const MOCK = { idle: 0, tapInstall: 1, installing: 2, installed: 3, tapOpen: 4, open: 5 }
const ROUTINES = [['Upper body', 'Bench Press (Barbell), Cable Seated Row, Dumbbell Seated Shoulder Press'], ['Lower body', 'Squat (Barbell), Deadlift (Barbell), Sled 45° Leg Press']]
const WORKOUT_TABS = ['Workout', 'History', 'Exercises']

/* The real Workout app's first screen: its tab bar, the New workout button and the saved routines. */
function WorkoutApp({ icon, app }) {
  return <div className="wt-mock__open">
    <div className="wt-wk__tabs">
      <AppIcon className="wt-mock__app" item={app ? { ...app, icon_url: app.icon_url || icon } : { slug: 'workout', icon_url: icon }} label="Workout" size={null} />
      {WORKOUT_TABS.map((tab, index) => <span key={tab} className={index === 0 ? 'is-active' : ''}>{tab}</span>)}
    </div>
    <div className="wt-wk__body">
      <span className="wt-wk__new">+ New workout</span>
      <div className="wt-wk__head"><strong>Routines</strong><small>{ROUTINES.length} saved</small><span className="wt-wk__ghost">+ New routine</span></div>
      {ROUTINES.map(([name, exercises], index) => <div key={name} className="wt-wk__row" style={{ '--i': index }}>
        <span><strong>{name}</strong><small>{exercises}</small></span>
        <span className="wt-wk__ghost">Edit</span><span className="wt-wk__start">Start</span>
      </div>)}
    </div>
  </div>
}

/* A close copy of the real App Store's Browse page. The same finger as every other guide demo taps
   Install, the app installs, it taps Open, and the app opens, on a loop. */
function StoreWindow({ store }) {
  const phase = useLoopingTimeline(MOCK_PHASES)
  const rootRef = useRef(null)
  const buttonRef = useRef(null)
  const [finger, setFinger] = useState(null)
  const aiming = phase === MOCK.tapInstall
  // Measured as the finger is about to appear, so it lands on the button at any shell zoom.
  useLayoutEffect(() => {
    if (aiming && rootRef.current && buttonRef.current) setFinger(fingerPath(buttonRef.current, rootRef.current))
  }, [aiming])
  const workout = STORE_WINDOW_APPS[STORE_WINDOW_APPS.length - 1]
  return <div ref={rootRef} className="wt-mockwrap">
    <div className="wt-mock" role="img" aria-label="Illustration of the App Store. Browse, Library, and Publish tabs, and a list of picks where Workout gets installed and then opened.">
      <div className="wt-mock__bar" aria-hidden="true">
        {store.storeApp ? <AppIcon className="wt-mock__app" item={store.storeApp} label="App Store" size={null} /> : <span className="wt-mock__app" />}
        <strong>App Store</strong>
        <span className="wt-mock__tabs"><b>Browse</b><span>Library</span><span>Publish</span></span>
      </div>
      <div className="wt-mock__head" aria-hidden="true"><strong>Our picks</strong><span>See all</span></div>
      <div className="wt-mock__grid" aria-hidden="true">
        {STORE_WINDOW_APPS.map(app => {
          const installed = app.owned || phase >= MOCK.installed
          const real = installedApp(store.apps, app.id)
          return <div key={app.id} className={`wt-mock__row${app.owned ? '' : ' is-target'}`}>
            <span className="wt-mock__icon">
              <AppIcon item={real ? { ...real, icon_url: real.icon_url || store.icons[app.id] } : { slug: app.id, icon_url: store.icons[app.id] }} label={app.name} size={null} />
              {installed && <i className="wt-mock__badge"><CheckIcon size={9} /></i>}
            </span>
            <span className="wt-mock__text"><strong>{app.name}</strong><small>{app.blurb}</small><em>mobius-os</em></span>
            <span ref={app.owned ? undefined : buttonRef} className="wt-mock__btn">{installed ? 'Open' : phase === MOCK.installing ? 'Installing…' : 'Install'}</span>
          </div>
        })}
      </div>
      {phase >= MOCK.open && <WorkoutApp icon={store.icons[workout.id]} app={installedApp(store.apps, workout.id)} />}
    </div>
    {phase >= MOCK.tapInstall && phase <= MOCK.installing && finger && <Touch key="install" x={finger.x} y={finger.y} from={finger.from} out={phase === MOCK.installing} />}
    {phase >= MOCK.tapOpen && phase <= MOCK.open && finger && <Touch key="open" x={finger.x} y={finger.y} second still out={phase === MOCK.open} />}
  </div>
}

export default function WalkthroughStore({ store }) {
  return <>
    <StoreWindow store={store} />
    <ul className="wt-next wt-next--below" aria-label="What you can do in App Store">
      {STORE_POINTS.map(point => <li key={point.id}><strong>{point.label}</strong><span>{point.text}</span></li>)}
    </ul>
  </>
}
